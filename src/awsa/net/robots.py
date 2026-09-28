"""``robots.txt`` parsing and enforcement.

Implements the matching rules from RFC 9309 that matter in practice: longest
match wins, ``Allow`` beats ``Disallow`` at equal length, ``$`` anchors the end
of the path, ``*`` matches any run of characters, and the most specific
user-agent group (rather than ``*``) applies when one matches.
"""

from __future__ import annotations

import contextlib
import re
from collections.abc import Iterable
from dataclasses import dataclass
from urllib.parse import unquote, urlsplit

__all__ = ["RobotsDecision", "RobotsPolicy", "RobotsRules"]


@dataclass(frozen=True, slots=True)
class RobotsRules:
    """The rule lines that apply to one user-agent group."""

    allow: tuple[str, ...] = ()
    disallow: tuple[str, ...] = ()
    crawl_delay: float | None = None

    def is_empty(self) -> bool:
        return not self.allow and not self.disallow


@dataclass(frozen=True, slots=True)
class RobotsPolicy:
    """Parsed ``robots.txt`` for a single origin, already resolved to one group."""

    rules: RobotsRules
    sitemaps: tuple[str, ...] = ()
    raw_bytes: int = 0
    available: bool = True
    matched_group: str = "*"


def _pattern_to_regex(pattern: str) -> re.Pattern[str]:
    anchored_end = pattern.endswith("$")
    core = pattern[:-1] if anchored_end else pattern
    escaped = re.escape(core)
    escaped = escaped.replace(r"\*", ".*")
    tail = "$" if anchored_end else ""
    return re.compile(f"^{escaped}{tail}")


def _length(path: str) -> int:
    return len(path)


def _decide(rules: Iterable[tuple[bool, str]], path: str) -> bool:
    best_allow: tuple[int, str] | None = None
    best_disallow: tuple[int, str] | None = None
    for is_allow, pattern in rules:
        if not pattern:
            continue
        try:
            regex = _pattern_to_regex(pattern)
        except re.error:
            continue
        if not regex.match(path):
            continue
        weight = _length(pattern)
        if is_allow:
            if best_allow is None or weight > best_allow[0]:
                best_allow = (weight, pattern)
        else:
            if best_disallow is None or weight > best_disallow[0]:
                best_disallow = (weight, pattern)
    if best_disallow is None:
        return True
    # Longest-match wins; an equally specific Allow beats a Disallow.
    return best_allow is not None and best_allow[0] >= best_disallow[0]


def parse_robots(text: str, *, user_agent: str = "*") -> RobotsPolicy:
    """Parse ``robots.txt`` content and return the policy for ``user_agent``.

    Args:
        text: Raw file contents.
        user_agent: Our product token; matched case-insensitively against
            ``User-agent`` groups, falling back to ``*``.

    Returns:
        A :class:`RobotsPolicy`. Malformed lines are skipped rather than
        raising, matching how real-world robots.txt files behave.
    """
    ua_token = user_agent.split("/", 1)[0].strip().lower() or "*"
    groups: list[tuple[list[str], list[tuple[bool, str]], float | None]] = []
    current_agents: list[str] = []
    current_rules: list[tuple[bool, str]] = []
    current_delay: float | None = None
    expecting_agents = False

    def flush() -> None:
        nonlocal current_agents, current_rules, current_delay, expecting_agents
        if current_agents or current_rules:
            groups.append((list(current_agents), list(current_rules), current_delay))
        current_agents = []
        current_rules = []
        current_delay = None
        expecting_agents = True

    for raw_line in text.splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line:
            continue
        if ":" not in line:
            continue
        field_name, _, value = line.partition(":")
        key = field_name.strip().lower()
        val = value.strip()

        if key == "user-agent":
            if not expecting_agents:
                flush()
            current_agents.append(val.lower())
            continue

        expecting_agents = False
        if key in {"allow", "disallow"}:
            current_rules.append((key == "allow", val))
        elif key == "crawl-delay":
            # A malformed delay means "no delay override", not a hard failure.
            with contextlib.suppress(ValueError):
                current_delay = float(val)
    flush()

    sitemaps = tuple(
        line.split(":", 1)[1].strip()
        for line in text.splitlines()
        if line.split(":", 1)[0].strip().lower() == "sitemap" and line.split(":", 1)[1].strip()
    )

    # Each entry records the *matched agent token* (a single str), its rules and
    # its crawl delay; the group's full agent list is not needed downstream.
    selected: list[tuple[str, list[tuple[bool, str]], float | None]] = []
    fallback: tuple[str, list[tuple[bool, str]], float | None] | None = None
    for agents, rules, delay in groups:
        if any(agent == "*" for agent in agents) and fallback is None:
            fallback = ("*", rules, delay)
        for agent in agents:
            if not agent:
                continue
            # RFC 9309 matches the product token case-insensitively, allowing a
            # crawler's version suffix to be ignored. A plain substring test
            # would let a "bot" group capture "Googlebot", so require that the
            # group token is followed by a non-alphanumeric boundary.
            if agent == ua_token or re.match(rf"{re.escape(agent)}(?![a-z0-9])", ua_token):
                selected.append((agent, rules, delay))
                break

    if selected:
        best_agent, rules, delay = max(selected, key=lambda item: len(item[0]))
    elif fallback is not None:
        _, rules, delay = fallback
        best_agent = "*"
    else:
        rules, delay, best_agent = [], None, "* (no groups)"

    return RobotsPolicy(
        rules=RobotsRules(
            allow=tuple(p for is_allow, p in rules if is_allow and p),
            disallow=tuple(p for is_allow, p in rules if not is_allow and p),
            crawl_delay=delay,
        ),
        sitemaps=sitemaps,
        raw_bytes=len(text.encode()),
        matched_group=best_agent,
    )


@dataclass(frozen=True, slots=True)
class RobotsDecision:
    """Why a given path was allowed or denied."""

    allowed: bool
    reason: str
    matched_rule: str | None = None


class RobotsCache:
    """Caches one :class:`RobotsPolicy` per origin for the process lifetime."""

    def __init__(self) -> None:
        self._by_origin: dict[str, RobotsPolicy] = {}

    def get(self, origin: str) -> RobotsPolicy | None:
        return self._by_origin.get(self._key(origin))

    def put(self, origin: str, policy: RobotsPolicy) -> None:
        self._by_origin[self._key(origin)] = policy

    def known(self, origin: str) -> bool:
        return self._key(origin) in self._by_origin

    @staticmethod
    def _key(origin: str) -> str:
        parts = urlsplit(origin)
        host = (parts.hostname or "").lower()
        port = parts.port or (443 if parts.scheme == "https" else 80)
        return f"{host}:{port}"


def decide(
    policy: RobotsPolicy,
    url: str,
) -> RobotsDecision:
    """Decide whether ``url`` may be fetched under an already-selected ``policy``.

    Empty ``Disallow:`` means "allow all" and is treated as such rather than as
    a rule that matches everything.
    """
    parts = urlsplit(url)
    path = unquote(parts.path or "/")
    if parts.query:
        path = f"{path}?{parts.query}"

    rules: list[tuple[bool, str]] = []
    if policy.rules.allow:
        rules.extend((True, p) for p in policy.rules.allow)
    if policy.rules.disallow:
        rules.extend((False, p) for p in policy.rules.disallow if p)

    if not rules:
        return RobotsDecision(True, "no applicable rules")

    allowed = _decide(rules, path)
    if allowed:
        matching = [p for is_allow, p in rules if is_allow and _pattern_to_regex(p).match(path)]
        return RobotsDecision(
            True, "allowed by longest matching rule", matching[0] if matching else None
        )
    matching = [p for is_allow, p in rules if not is_allow and _pattern_to_regex(p).match(path)]
    longest = max(matching, key=len) if matching else None
    return RobotsDecision(False, f"disallowed by {longest!r}", longest)
