"""Network layer: SSRF guard, robots, pacing, caching and HTTP."""

from __future__ import annotations

from .cache import CacheEntry, HttpCache, MemoryCache, ResponseCache
from .guard import SSRFGuard, UrlVerdict, is_public_address
from .http import HttpFetcher
from .ratelimit import HostPacer
from .robots import RobotsDecision, RobotsPolicy, RobotsRules, decide, parse_robots

__all__ = [
    "CacheEntry",
    "HostPacer",
    "HttpCache",
    "HttpFetcher",
    "MemoryCache",
    "ResponseCache",
    "RobotsDecision",
    "RobotsPolicy",
    "RobotsRules",
    "SSRFGuard",
    "UrlVerdict",
    "decide",
    "is_public_address",
    "parse_robots",
]
