"""Shared fixtures.

The suite runs against a real HTTP server on loopback rather than a mocking
framework. Redirects, ``Retry-After``, robots parsing and the byte cap are
behaviours of the server, not of a stub, so faking them would test the mock
instead of the code. The SSRF guard is opened to private hosts *only* for these
tests, which is the one concession made to get a real socket.
"""

from __future__ import annotations

import sys
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, ClassVar

import pytest

from awsa.config import Config, ExtractionSettings, NetworkSettings, ProviderSettings
from awsa.models import Budget

# A client that hits a byte cap, times out, or cancels closes the socket
# abruptly. Windows surfaces that as ConnectionAbortedError (WinError 10053)
# rather than ConnectionResetError, so catch the whole family.
_CONNECTION_GONE = (BrokenPipeError, ConnectionResetError, ConnectionAbortedError)


def _quiet_handle_error(
    request: object,
    client_address: object,
) -> None:
    """Swallow client-disconnect noise, re-raise anything genuine."""
    exc = sys.exc_info()[1]
    if isinstance(exc, _CONNECTION_GONE):
        return
    raise exc if exc is not None else AssertionError


# -- fixture pages ---------------------------------------------------------

PEOPLE_HTML = """<!doctype html>
<html><head>
  <title>Ada Lovelace - Example Biographies</title>
  <meta name="description" content="Ada Lovelace was an English mathematician.">
  <script type="application/ld+json">
  {"@type":"Person","name":"Ada Lovelace","birthDate":"1815-12-10",
   "birthPlace":{"@type":"Place","name":"London"},
   "jobTitle":"Mathematician"}
  </script>
  <style>body{color:red}.x{display:none}</style>
</head>
<body>
  <nav><a href="/other">Other</a><a href="/help">Help</a></nav>
  <div id="navmenu"><a href="/junk">Junk</a></div>
  <main>
    <h1>Ada Lovelace</h1>
    <p>Ada Lovelace was an English mathematician and writer, chiefly known for
    her work on Charles Babbage's proposed Analytical Engine.</p>
    <p>She was born on 10 December 1815 in London, England, and was the daughter
    of the poet Lord Byron and Anne Isabella Milbanke.</p>
    <p>Lovelace died on 27 November 1852, in Marylebone, London.</p>
    <a href="/lovelace-notes">Her notes on the Analytical Engine</a>
  </main>
  <footer><a href="/terms">Terms</a><a href="/privacy">Privacy</a></footer>
</body></html>
"""

ROBOTS = """User-agent: *
Disallow: /private/
Disallow: /admin

User-agent: BadBot
Disallow: /

Sitemap: https://{host}/sitemap.xml
"""

CHALLENGE_HTML = """<html><body>
<script src="/anomaly.js"></script>
<p>Unfortunately, bots use DuckDuckGo too.</p>
</body></html>
"""


@dataclass
class ServerState:
    """Mutable per-server behaviour, switched by tests through ``handler``."""

    robots: str = ""
    challenge: bool = False
    requests: list[str] = None  # type: ignore[assignment]
    hits: dict[str, int] = None  # type: ignore[assignment]
    flaky_remaining: int = 0
    huge_bytes: int = 0
    etag: str | None = '"v1"'
    last_modified: str | None = None


class FixtureHandler(BaseHTTPRequestHandler):
    """Serves the fixture pages. ``protocol_version`` keeps keep-alive working."""

    protocol_version = "HTTP/1.1"
    state: ClassVar[ServerState]

    def log_message(self, fmt: str, *args: Any) -> None:
        """Silence the default stderr access log."""

    # -- helpers ------------------------------------------------------------

    def _send(
        self,
        status: int,
        body: bytes | str = b"",
        content_type: str = "text/html; charset=utf-8",
        extra: dict[str, str] | None = None,
    ) -> None:
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if body:
            try:
                self.wfile.write(body)
            except _CONNECTION_GONE:
                # Client hung up (byte cap, timeout, cancelled request).
                return

    def _path(self) -> str:
        return self.path.split("?", 1)[0]

    # -- verbs --------------------------------------------------------------

    def do_GET(self) -> None:
        path = self._path()
        self.state.requests.append(self.path)
        self.state.hits[path] = self.state.hits.get(path, 0) + 1

        if path == "/robots.txt":
            self._send(200, self.state.robots or ROBOTS.format(host=""))
            return

        if path == "/flaky":
            if self.state.flaky_remaining > 0:
                self.state.flaky_remaining -= 1
                self._send(503, "slow down", extra={"Retry-After": "0"})
                return
            # Once throttling stops, serve the real page so a retry can succeed.
            self._send(200, "<html><body><h1>Recovered</h1></body></html>")
            return

        if path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "/people")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        if path == "/redirect-external":
            self.send_response(302)
            self.send_header("Location", "http://169.254.169.254/latest/meta-data/")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        if path == "/redirect-loop":
            self.send_response(302)
            self.send_header("Location", "/redirect-loop")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        if path == "/huge":
            size = self.state.huge_bytes or 50_000_000
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(size))
            self.end_headers()
            remaining = size
            chunk = b"x" * 65536
            while remaining > 0:
                try:
                    self.wfile.write(chunk[: min(len(chunk), remaining)])
                except _CONNECTION_GONE:
                    # The client hit its byte cap and hung up mid-stream. That
                    # is the behaviour under test, not a server failure.
                    return
                remaining -= len(chunk)
            return

        if path == "/challenge":
            self._send(200, CHALLENGE_HTML)
            return

        if path in {"/people", "/people/", "/", "/index.html"}:
            extra: dict[str, str] = {}
            if self.headers.get("If-None-Match") == '"v1"':
                self._send(304, b"", extra=extra)
                return
            if self.state.etag:
                extra["ETag"] = self.state.etag
            self._send(200, PEOPLE_HTML, extra=extra)
            return

        self._send(404, "not found")

    def do_HEAD(self) -> None:
        self.do_GET()


# -- fixtures --------------------------------------------------------------


@pytest.fixture
def server() -> Iterator[FixtureServer]:
    """A real HTTP server on loopback serving the fixture pages."""
    state = ServerState(requests=[], hits={})
    FixtureHandler.state = state
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), FixtureHandler)
    # Backstop: a client that vanishes mid-response must not print a traceback
    # into the test output. Individual handlers already absorb these; this
    # catches anything raised from the socket layer itself.
    httpd.handle_error = _quiet_handle_error  # type: ignore[method-assign]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    host, port = httpd.server_address[0], httpd.server_address[1]
    # server_address is typed as a generic address; for an AF_INET bind the two
    # elements are str and int respectively.
    fixture = FixtureServer(f"http://{host}:{port}", state)  # type: ignore[str-bytes-safe]
    try:
        yield fixture
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


@dataclass
class FixtureServer:
    base: str
    state: ServerState

    def url(self, path: str = "/") -> str:
        return f"{self.base}{path}"


@pytest.fixture
def config(server: FixtureServer) -> Config:
    """Production config pointed at the fixture server.

    Private hosts are permitted because the server is on loopback, zero delays
    keep the suite fast, and the cache is disabled so each test sees real
    requests.
    """
    return Config(
        network=NetworkSettings(
            user_agent="awsa-test/0.1",
            allow_private_hosts=True,
            respect_robots=True,
            per_host_delay_seconds=0.0,
            backoff_base_seconds=0.01,
            backoff_max_seconds=0.05,
            max_page_bytes=1_000_000,
            max_retries=2,
        ),
        providers=ProviderSettings(llm_provider="extractive", search_provider="duckduckgo"),
        extraction=ExtractionSettings(),
        log_level="CRITICAL",
    )


@pytest.fixture
def fast_budget() -> Budget:
    return Budget.preset("tiny")
