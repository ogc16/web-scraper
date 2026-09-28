"""Per-host politeness: minimum interval between requests to the same host.

Robots.txt ``Crawl-delay`` is advisory and often absent, so politeness must be
enforced by the client regardless. Implemented as an async lock plus a
"next allowed time" timestamp per host, with jittered backoff on 429/503.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Iterable
from dataclasses import dataclass, field

__all__ = ["HostPacer", "Pacer"]


@dataclass(slots=True)
class _HostState:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    next_allowed: float = 0.0
    consecutive_throttles: int = 0


class HostPacer:
    """Tracks a minimum request interval per host and absorbs throttle signals."""

    def __init__(
        self,
        *,
        min_interval: float = 1.0,
        max_interval: float = 60.0,
        jitter: float = 0.25,
        seed: int | None = None,
    ) -> None:
        if min_interval < 0:
            msg = "min_interval must be >= 0"
            raise ValueError(msg)
        self.min_interval = min_interval
        self.max_interval = max_interval
        self.jitter = jitter
        self._hosts: dict[str, _HostState] = {}
        # Jitter only: spreads retries so we do not stampede a host. Seedable so
        # tests are deterministic. Never used for anything security-relevant.
        self._rng = random.Random(seed)  # noqa: S311  (timing jitter, not crypto)
        self._loop: asyncio.AbstractEventLoop | None = None
        self.slept_seconds = 0.0

    def _now(self) -> float:
        loop = asyncio.get_running_loop()
        return loop.time()

    def _state(self, host: str) -> _HostState:
        state = self._hosts.get(host)
        if state is None:
            state = _HostState()
            self._hosts[host] = state
        return state

    async def acquire(self, host: str, *, extra_delay: float = 0.0) -> float:
        """Block until it is polite to hit ``host``. Returns seconds slept."""
        state = self._state(host.lower())
        async with state.lock:
            now = self._now()
            # A pending cooldown counts even when the polite interval is zero.
            # Short-circuiting on min_interval alone would let a server's
            # Retry-After be ignored by anyone who disabled pacing.
            cooldown = max(0.0, state.next_allowed - now)
            wait = cooldown + max(0.0, extra_delay)
            if wait <= 0.0 and self.min_interval <= 0.0:
                return 0.0
            if wait > 0:
                self.slept_seconds += wait
                await asyncio.sleep(wait)
            interval = self.min_interval
            if interval > 0:
                spread = interval * self.jitter
                interval += self._rng.uniform(-spread, spread)
            # Keep any cooldown still running; only extend it when pacing is on.
            state.next_allowed = max(state.next_allowed, self._now() + max(0.0, interval))
            state.consecutive_throttles = 0
        return wait

    def penalize(self, host: str, *, retry_after: float | None = None) -> float:
        """Back off ``host`` after a 429/503. Returns the delay now in force."""
        state = self._state(host.lower())
        state.consecutive_throttles += 1
        if retry_after is not None:
            delay = max(0.0, retry_after)
        else:
            delay = min(
                self.max_interval,
                self.min_interval * (2 ** (state.consecutive_throttles - 1)),
            )
        delay = min(self.max_interval, delay * (1 + self.jitter * 0.5))
        state.next_allowed = max(state.next_allowed, self._now() + delay)
        return delay

    def relax(self, host: str) -> None:
        """Clear throttle state after a successful response.

        A 2xx is direct evidence the host is not throttling us, so it drops both
        the counter and any cooldown still pending. Without this a single
        earlier 503 would keep delaying every later request to a host that has
        since recovered.
        """
        state = self._hosts.get(host.lower())
        if state is not None:
            state.consecutive_throttles = 0
            state.next_allowed = min(state.next_allowed, self._now())

    def observe_robots_delay(self, host: str, crawl_delay: float | None) -> None:
        """Raise the floor for ``host`` if robots.txt asks for a longer delay."""
        if crawl_delay is None or crawl_delay <= 0:
            return
        state = self._state(host.lower())
        state.next_allowed = max(state.next_allowed, self._now() + crawl_delay)

    def stats(self) -> dict[str, float | int]:
        return {
            "hosts_tracked": len(self._hosts),
            "slept_seconds": round(self.slept_seconds, 3),
        }


Pacer = HostPacer


def normalize_hosts(hosts: Iterable[str]) -> frozenset[str]:
    """Lowercase and strip a host iterable, dropping empties."""
    return frozenset(h.strip().lower().rstrip(".") for h in hosts if h and h.strip())
