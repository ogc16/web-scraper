"""Logging, secret redaction and lightweight run tracing.

Every log record passes through :class:`RedactingFilter` so an accidental
``logger.info("token=%s", key)`` can never leak a credential into CI output.
"""

from __future__ import annotations

import logging
import re
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final, Self

from .config import _scrub

if TYPE_CHECKING:
    from typing import TextIO

__all__ = [
    "RedactingFilter",
    "Span",
    "Trace",
    "configure_logging",
    "get_logger",
]

_LOGGER_NAMESPACE: Final = "awsa"

_SECRETISH: Final = re.compile(
    r"(?i)\b(api[_-]?key|api[_-]?token|token|password|passwd|secret|authorization|bearer)\b"
    r"(\s*[:=]\s*)(\"|')?([^\s\"',}]{4,})"
)

_ENV_ASSIGNMENT: Final = re.compile(
    r"\b(AWS_[A-Z0-9_]*|[A-Z0-9_]*(?:KEY|TOKEN|SECRET|PASSWORD))\b(\s*=\s*)(\S+)"
)


def _redact_message(message: str) -> str:
    message = _SECRETISH.sub(lambda m: f"{m.group(1)}{m.group(2)}[REDACTED]", message)
    message = _ENV_ASSIGNMENT.sub(lambda m: f"{m.group(1)}{m.group(2)}[REDACTED]", message)
    return _scrub(message)


class RedactingFilter(logging.Filter):
    """Strips credential-shaped substrings from every log record.

    Registered automatically by :func:`configure_logging` on the ``awsa``
    logger, and applied to records propagated from child loggers.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            rendered = record.getMessage()
        except Exception:  # pragma: no cover - defensive, malformed %-args
            return True
        cleaned = _redact_message(rendered)
        if cleaned != rendered:
            record.msg = cleaned
            record.args = ()
        return True


_configured = False


def configure_logging(
    level: str = "INFO",
    *,
    stream: TextIO | None = None,
    force: bool = False,
) -> None:
    """Attach a redacting stderr handler to the ``awsa`` logger exactly once."""
    global _configured
    logger = logging.getLogger(_LOGGER_NAMESPACE)
    numeric = getattr(logging, level.upper(), logging.INFO)
    logger.setLevel(numeric)
    if _configured and not force:
        return
    handler = logging.StreamHandler(stream or sys.stderr)
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%H:%M:%S")
    )
    handler.addFilter(RedactingFilter())
    for existing in list(logger.handlers):
        logger.removeHandler(existing)
    logger.addHandler(handler)
    logger.propagate = False
    _configured = True


def get_logger(name: str) -> logging.Logger:
    """Return a child logger under the ``awsa`` namespace."""
    if not name.startswith(f"{_LOGGER_NAMESPACE}."):
        name = f"{_LOGGER_NAMESPACE}.{name}" if name != _LOGGER_NAMESPACE else _LOGGER_NAMESPACE
    return logging.getLogger(name)


@dataclass(slots=True)
class Span:
    """A single timed step of a run, for verbose progress output."""

    name: str
    started: float = field(default_factory=time.perf_counter)
    detail: dict[str, Any] = field(default_factory=dict)
    duration_ms: int = 0

    def finish(self) -> int:
        self.duration_ms = int((time.perf_counter() - self.started) * 1000)
        return self.duration_ms


@dataclass(slots=True)
class Trace:
    """Collects :class:`Span` objects and prints them as an aligned summary."""

    enabled: bool = True
    spans: list[Span] = field(default_factory=list)
    _active: list[Span] = field(default_factory=list)

    @contextmanager
    def span(self, name: str, **detail: Any) -> Iterator[Span]:
        span = Span(name=name, detail=detail)
        self.spans.append(span)
        self._active.append(span)
        try:
            yield span
        finally:
            self._active.pop()
            span.finish()

    def record(self, name: str, ms: int, **detail: Any) -> None:
        self.spans.append(Span(name=name, detail=detail, duration_ms=ms))

    def render(self) -> str:
        if not self.spans:
            return ""
        width = max(len(s.name) for s in self.spans)
        lines = []
        for span in self.spans:
            suffix = " ".join(f"{k}={v}" for k, v in span.detail.items())
            lines.append(f"  {span.name.ljust(width)}  {span.duration_ms:>6}ms  {suffix}".rstrip())
        return "\n".join(lines)

    def total_ms(self) -> int:
        return sum(s.duration_ms for s in self.spans)

    def merge(self, other: Trace) -> Self:
        self.spans.extend(other.spans)
        return self
