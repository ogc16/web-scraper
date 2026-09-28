"""Command-line interface.

Subcommands:

* ``research`` — run the agent and print a report. The default action.
* ``providers`` — show which providers resolved and why.
* ``fetch`` — fetch one URL and print its readable text. The debugging tool.
* ``robots`` — evaluate a URL against a site's robots.txt.
* ``doctor`` — verify the environment can make a request at all.

Exit codes are meaningful: ``0`` success, ``1`` nothing found, ``2`` bad
usage or configuration, ``3`` blocked by policy, ``4`` all providers failed.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import sys
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, Final

if TYPE_CHECKING:
    from typing import TextIO

from . import __version__
from .agent import AutonomousScraperAgent, ResearchSpec
from .config import Config
from .errors import AwsaError, ConfigError, NetworkBlocked, RobotsDenied, UnsafeURLError
from .extract import read_page
from .models import Budget
from .net import HttpFetcher, SSRFGuard, decide
from .net.client import build_async_client
from .net.robots import parse_robots
from .observability import configure_logging, get_logger
from .providers import Registry
from .report import render

__all__ = ["build_parser", "main"]

log = get_logger("cli")

EXIT_OK: Final = 0
EXIT_EMPTY: Final = 1
EXIT_USAGE: Final = 2
EXIT_BLOCKED: Final = 3
EXIT_FAILED: Final = 4
_EXIT_CODES: Final = (EXIT_OK, EXIT_EMPTY, EXIT_USAGE, EXIT_BLOCKED, EXIT_FAILED)

_EPILOG: Final = """\
examples:
  awsa research "Grace Hopper" --fields name,bio,birth_date
  awsa research "Jane Doe" -f city -f employer -f json
  awsa research "Acme Corp" -f "founded:when founded" --budget deep
  awsa research "Ada Lovelace" --offline --cache-dir .cache
  awsa providers --json
  awsa fetch https://example.com
  awsa robots https://example.com/some/path
  awsa doctor

Run with zero configuration: no API key is required. Set AWSA_OPENAI_API_KEY
to use a hosted model, AWSA_BRAVE_API_KEY for a reliable search backend.
"""


def _budget_from(value: str) -> Budget:

    try:
        return Budget.preset(value)
    except ValueError as exc:
        raise ConfigError(str(exc)) from exc


def _split_fields(values: Sequence[str] | None, inline: str | None) -> list[str]:
    fields: list[str] = []
    for chunk in values or ():
        fields.extend(part.strip() for part in chunk.split(",") if part.strip())
    if inline:
        fields.extend(part.strip() for part in inline.split(",") if part.strip())
    return fields


DEFAULT_FIELDS: Final = ("name", "bio")


def build_parser() -> argparse.ArgumentParser:
    """Construct the argument parser. Exposed for testing and shell completion."""
    parser = argparse.ArgumentParser(
        prog="awsa",
        description="Autonomous, evidence-grounded web research agent.",
        epilog=_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"awsa {__version__}")
    sub = parser.add_subparsers(dest="command")

    def add_common(target: argparse.ArgumentParser) -> None:
        target.add_argument(
            "--log-level",
            default=None,
            choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
            help="stderr log verbosity (default: INFO)",
        )
        target.add_argument(
            "--offline",
            action="store_true",
            help="use no hosted or paid providers; keyless network search still runs",
        )
        target.add_argument(
            "--no-network",
            action="store_true",
            help="open no sockets at all; answer only from a warm cache",
        )
        target.add_argument("--cache-dir", default=None, help="persist the HTTP cache here")
        target.add_argument("--cache-ttl", type=float, default=None, help="cache freshness seconds")
        target.add_argument(
            "--ignore-robots",
            action="store_true",
            help="do not fetch or apply robots.txt (use only where you are authorised)",
        )
        target.add_argument(
            "--per-host-delay", type=float, default=None, help="minimum seconds between hits"
        )
        target.add_argument(
            "--timeout", type=float, default=None, help="per-request timeout seconds"
        )
        target.add_argument(
            "--search-provider",
            default=None,
            help="auto (default), duckduckgo, brave, brightdata-serp",
        )
        target.add_argument(
            "--llm-provider", default=None, help="auto (default), openai, extractive"
        )
        target.add_argument("--user-agent", default=None, help="User-Agent header to send")

    research = sub.add_parser(
        "research",
        help="research a subject and print an evidence-grounded report",
        description="Research a subject and print an evidence-grounded report.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    research.add_argument("subject", help="who or what to research")
    research.add_argument(
        "-f",
        "--field",
        action="append",
        dest="fields",
        metavar="NAME[:HINT]",
        help="field to extract; repeatable and comma-separated. Default: name,bio",
    )
    research.add_argument(
        "--budget",
        default="standard",
        choices=["tiny", "standard", "deep", "none"],
        help="resource ceiling (default: standard)",
    )
    research.add_argument(
        "--format",
        default="markdown",
        choices=["markdown", "json", "plain"],
        help="output format (default: markdown)",
    )
    research.add_argument("-o", "--output", default=None, help="write the report to this file")
    research.add_argument(
        "--include-domain",
        action="append",
        default=[],
        metavar="HOST",
        help="only use evidence from this host; repeatable",
    )
    research.add_argument(
        "--exclude-domain",
        action="append",
        default=[],
        metavar="HOST",
        help="never use evidence from this host; repeatable",
    )
    research.add_argument(
        "--max-pages-per-domain", type=int, default=None, help="cap pages read per host"
    )
    research.add_argument(
        "--min-sources",
        type=int,
        default=None,
        help="independent domains required before a field counts as corroborated",
    )
    research.add_argument(
        "--no-stop-on-coverage", action="store_true", help="keep reading until the budget runs out"
    )
    research.add_argument("--trace", action="store_true", help="print per-stage timings to stderr")
    add_common(research)

    providers = sub.add_parser("providers", help="show which providers resolved and why")
    providers.add_argument("--json", action="store_true", help="machine-readable output")
    add_common(providers)

    fetch = sub.add_parser("fetch", help="fetch one URL and print its readable text")
    fetch.add_argument("url")
    fetch.add_argument("--raw", action="store_true", help="print raw HTML instead of reduced text")
    fetch.add_argument("--json", action="store_true", help="print extraction metadata as JSON")
    add_common(fetch)

    robots = sub.add_parser("robots", help="check a URL against the site's robots.txt")
    robots.add_argument("url")
    add_common(robots)

    doctor = sub.add_parser("doctor", help="check that the environment can make requests")
    add_common(doctor)

    return parser


def _config_from(args: argparse.Namespace) -> Config:
    from dataclasses import replace

    base = Config.from_env(os.environ)

    network_updates: dict[str, Any] = {}
    if getattr(args, "cache_dir", None):
        from pathlib import Path

        network_updates["cache_dir"] = Path(args.cache_dir)
    if getattr(args, "cache_ttl", None) is not None:
        network_updates["cache_ttl_seconds"] = args.cache_ttl
    if getattr(args, "ignore_robots", False):
        network_updates["respect_robots"] = False
    if getattr(args, "per_host_delay", None) is not None:
        network_updates["per_host_delay_seconds"] = args.per_host_delay
    if getattr(args, "timeout", None) is not None:
        network_updates["timeout_seconds"] = args.timeout
    if getattr(args, "user_agent", None):
        network_updates["user_agent"] = args.user_agent

    provider_updates: dict[str, Any] = {}
    if getattr(args, "search_provider", None):
        provider_updates["search_provider"] = args.search_provider
    if getattr(args, "llm_provider", None):
        provider_updates["llm_provider"] = args.llm_provider

    return replace(
        base,
        network=replace(base.network, **network_updates) if network_updates else base.network,
        providers=replace(base.providers, **provider_updates)
        if provider_updates
        else base.providers,
        log_level=(getattr(args, "log_level", None) or base.log_level).upper(),
        offline=bool(getattr(args, "offline", False)) or base.offline,
        network_enabled=not bool(getattr(args, "no_network", False)) and base.network_enabled,
    )


def _env_with_prefix() -> dict[str, str]:
    return dict(os.environ)


# -- commands --------------------------------------------------------------


async def _cmd_research(args: argparse.Namespace, out: TextIO, err: TextIO) -> int:
    fields = _split_fields(args.fields, None)
    if not fields:
        fields = list(DEFAULT_FIELDS)
        print(f"note: no --field given, defaulting to {','.join(DEFAULT_FIELDS)}", file=err)

    spec = ResearchSpec.build(
        args.subject,
        fields,
        budget=_budget_from(args.budget),
        include_domains=frozenset(args.include_domain or ()),
        exclude_domains=frozenset(args.exclude_domain or ()),
        **(
            {"max_pages_per_domain": args.max_pages_per_domain} if args.max_pages_per_domain else {}
        ),
        **({"min_independent_sources": args.min_sources} if args.min_sources else {}),
    )

    config = _config_from(args)
    agent = AutonomousScraperAgent(config, spec)
    result = await agent.run(stop_on_coverage=not args.no_stop_on_coverage)

    if args.trace:
        print(result.trace.render(), file=err)
        print(f"  {'TOTAL':<24} {result.trace.total_ms():>6}ms", file=err)

    text = render(result.report, args.format)
    if args.output:
        from pathlib import Path

        # Disk write, not network: off the event loop so a slow or contended
        # filesystem cannot stall in-flight fetches.
        await asyncio.to_thread(Path(args.output).write_text, text, encoding="utf-8")
        print(f"wrote {args.output}", file=err)
    else:
        print(text, file=out)

    for note in result.notes:
        print(f"note: {note}", file=err)
    print(
        f"stopped: {result.stopped_because} · coverage {result.report.coverage:.0%}",
        file=err,
    )
    return EXIT_OK if result.report.answered else EXIT_EMPTY


async def _cmd_providers(args: argparse.Namespace, out: TextIO, _err: TextIO) -> int:
    config = _config_from(args)
    registry = Registry(config)
    described = registry.describe()
    try:
        if args.json:
            print(json.dumps(described, indent=2), file=out)
        else:
            print("search providers (in fallback order):", file=out)
            for entry in described["search"]:
                key = "key required" if entry["requires_key"] else "no key needed"
                print(f"  - {entry['name']}  [{key}]", file=out)
            print("llm providers (in fallback order):", file=out)
            for entry in described["llm"]:
                key = "key required" if entry["requires_key"] else "no key needed"
                print(f"  - {entry['name']}  [{key}]", file=out)
            if described["notes"]:
                print("\nresolution notes:", file=out)
                for note in described["notes"]:
                    print(f"  - {note}", file=out)
    finally:
        await registry.aclose()
    return EXIT_OK


async def _cmd_fetch(args: argparse.Namespace, out: TextIO, err: TextIO) -> int:
    config = _config_from(args)
    fetcher = HttpFetcher(config)
    async with fetcher:
        try:
            result = await fetcher.fetch(args.url)
        except (RobotsDenied, UnsafeURLError, NetworkBlocked) as exc:
            # A policy said no, as opposed to the network failing. Keying this
            # off the exception type rather than its message keeps a changed
            # wording from silently reclassifying a refusal as a crash.
            print(f"error: {exc}", file=err)
            return EXIT_BLOCKED
        except AwsaError as exc:
            print(f"error: {exc}", file=err)
            return EXIT_FAILED

    if not result.ok:
        print(f"error: HTTP {result.status}", file=err)
        return EXIT_FAILED

    if args.raw:
        print(result.body.decode("utf-8", errors="replace"), file=out)
        return EXIT_OK

    content = read_page(result.final_url, result.body, config.extraction)
    if args.json:
        print(
            json.dumps(
                {
                    "url": result.final_url,
                    "status": result.status,
                    "title": content.title,
                    "description": content.description,
                    "strategy": content.strategy,
                    "score": content.score,
                    "word_count": content.word_count,
                    "truncated": content.truncated,
                    "links": list(content.links[:50]),
                    "from_cache": result.from_cache,
                    "text": content.text,
                },
                indent=2,
                ensure_ascii=False,
            ),
            file=out,
        )
    else:
        print(f"# {content.title or result.final_url}\n", file=out)
        if content.description:
            print(f"> {content.description}\n", file=out)
        print(content.text, file=out)
        print(
            f"\n---\n{content.word_count} words · strategy={content.strategy} "
            f"· score={content.score} · cache={'hit' if result.from_cache else 'miss'}",
            file=err,
        )
    return EXIT_OK


async def _cmd_robots(args: argparse.Namespace, out: TextIO, err: TextIO) -> int:
    config = _config_from(args)
    verdict = SSRFGuard(allow_private_hosts=config.network.allow_private_hosts).check(args.url)
    if not verdict.allowed:
        print(f"blocked by SSRF guard: {verdict.reason}", file=err)
        return EXIT_BLOCKED

    from urllib.parse import urlsplit

    import httpx

    parts = urlsplit(args.url)
    robots_url = f"{parts.scheme}://{parts.netloc}/robots.txt"
    async with build_async_client(
        network_enabled=config.network_enabled,
        timeout=15.0,
        headers={"User-Agent": config.network.user_agent},
    ) as client:
        try:
            response = await client.get(robots_url)
        except NetworkBlocked as exc:
            print(f"error: {exc}", file=err)
            return EXIT_BLOCKED
        except httpx.HTTPError as exc:
            print(f"error: could not fetch {robots_url}: {exc}", file=err)
            return EXIT_FAILED

    if response.status_code >= 400:
        print(f"{robots_url}: HTTP {response.status_code} — treated as allow-all", file=out)
        return EXIT_OK

    policy = parse_robots(response.text, user_agent=config.network.user_agent)
    decision = decide(policy, args.url)
    print(f"robots.txt: {robots_url}", file=out)
    print(f"matched group: {policy.matched_group}", file=out)
    print(
        f"allow rules: {len(policy.rules.allow)}  disallow rules: {len(policy.rules.disallow)}",
        file=out,
    )
    if policy.rules.crawl_delay:
        print(f"crawl-delay: {policy.rules.crawl_delay}", file=out)
    if policy.sitemaps:
        print(f"sitemaps: {len(policy.sitemaps)}", file=out)
    print("", file=out)
    verdict_word = "ALLOWED" if decision.allowed else "DENIED"
    print(f"{args.url}: {verdict_word} ({decision.reason})", file=out)
    return EXIT_OK if decision.allowed else EXIT_BLOCKED


async def _cmd_doctor(args: argparse.Namespace, out: TextIO, _err: TextIO) -> int:
    import httpx

    config = _config_from(args)
    ok = True
    print(f"awsa {__version__}", file=out)
    print(f"python {sys.version.split()[0]}", file=out)
    print("", file=out)

    registry = Registry(config)
    described = registry.describe()
    try:
        print("providers:", file=out)
        for entry in described["search"] + described["llm"]:
            print(f"  {entry['name']}", file=out)
        for note in described["notes"]:
            print(f"  note: {note}", file=out)
    finally:
        await registry.aclose()

    print("", file=out)
    if not config.network_enabled:
        # Being offline is what the caller asked for, so it is not a fault.
        # Reporting it as a failure would train people to ignore this line.
        print("network: disabled (--no-network); cache-only runs are supported", file=out)
        print("", file=out)
        print("ready.", file=out)
        return EXIT_OK

    try:
        async with build_async_client(
            network_enabled=config.network_enabled,
            timeout=15.0,
            headers={"User-Agent": config.network.user_agent},
        ) as client:
            response = await client.get("https://example.com")
        if response.status_code < 400:
            print(f"network: ok (example.com -> HTTP {response.status_code})", file=out)
        else:
            print(f"network: unexpected HTTP {response.status_code}", file=out)
            ok = False
    except httpx.HTTPError as exc:
        print(f"network: FAILED ({exc})", file=out)
        ok = False

    print("", file=out)
    print("ready." if ok else "not ready: network access is required", file=out)
    return EXIT_OK if ok else EXIT_FAILED


_COMMANDS: Final = {
    "research": _cmd_research,
    "providers": _cmd_providers,
    "fetch": _cmd_fetch,
    "robots": _cmd_robots,
    "doctor": _cmd_doctor,
}


def _normalise_argv(argv: Sequence[str]) -> list[str]:
    """Allow ``awsa "Grace Hopper"`` as shorthand for ``awsa research ...``.

    The first positional becomes a subject when it is not a known subcommand
    and does not look like a flag, which is what a user almost always means.
    Consecutive bare words are joined into one subject, so the unquoted
    ``awsa Grace Hopper`` means the same as ``awsa "Grace Hopper"`` instead of
    failing with a confusing "unrecognized arguments: Hopper".
    """
    args = list(argv)
    if not args:
        return args
    first = args[0]
    if first in _COMMANDS or first in {"-h", "--help", "--version"}:
        return args
    if first.startswith("-"):
        return args
    subject_parts: list[str] = []
    index = 0
    while index < len(args) and not args[index].startswith("-"):
        subject_parts.append(args[index])
        index += 1
    return ["research", " ".join(subject_parts), *args[index:]]


def _use_utf8_streams() -> None:
    """Force UTF-8 on the console.

    Reports contain typographic punctuation. On a Windows console the default
    encoding is cp1252, where an em dash raises ``UnicodeEncodeError`` and
    takes the whole run down — so the report would be lost to a character.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            with contextlib.suppress(ValueError, OSError):  # detached stream
                reconfigure(encoding="utf-8", errors="replace")


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point. Returns a process exit code rather than calling ``exit``."""
    _use_utf8_streams()
    raw = list(argv) if argv is not None else sys.argv[1:]
    if not raw:
        build_parser().print_help()
        return EXIT_USAGE

    parser = build_parser()
    try:
        args = parser.parse_args(_normalise_argv(raw))
    except SystemExit as exc:
        # argparse exits directly on a parse error (2) or on --help/--version
        # (0). Absorb it so main() always *returns* an exit code as documented
        # rather than unwinding through an exception, and map the code onto our
        # own vocabulary so callers see EXIT_USAGE rather than a bare 2.
        code = exc.code if isinstance(exc.code, int) else EXIT_USAGE
        return code if code in _EXIT_CODES else EXIT_USAGE
    command = args.command or "research"

    if not hasattr(args, "fields"):
        args.fields = None

    try:
        config = _config_from(args)
        configure_logging(config.log_level)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_USAGE

    handler = _COMMANDS[command]
    try:
        return asyncio.run(handler(args, sys.stdout, sys.stderr))
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return EXIT_FAILED
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_USAGE
    except AwsaError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_FAILED


if __name__ == "__main__":
    raise SystemExit(main())
