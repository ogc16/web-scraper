# Architecture

## Layering

Dependencies point downward only. Nothing in a lower layer imports a higher one.

```
                    ┌───────────────┐
                    │     cli.py    │  argparse surface, exit codes
                    └───────┬───────┘
                            │
                    ┌───────▼───────┐
                    │    report.py  │  markdown / json / plain
                    └───────▲───────┘
                            │
             ┌──────────────┴──────────────┐
             │        agent/              │
             │  spec · loop · verifier     │  orchestration, no I/O of its own
             └───────▲──────────────▲──────┘
                     │              │
        ┌────────────┴───┐   ┌──────┴──────────────────┐
        │    extract/    │   │      providers/         │
        │ html · reader  │   │ base · registry · impls │  one Protocol each
        └─────────▲──────┘   └───────────▲─────────────┘
                  │                      │
             ┌────┴──────────────────────┴────┐
             │            net/                │
             │ guard · robots · ratelimit ·    │  the only sockets
             │ cache · http                     │
             └──────────────▲──────────────────┘
                            │
                    ┌───────┴────────┐
                    │  config/models │  frozen value types
                    └────────────────┘
```

## The three seams

`src/awsa/providers/base.py` defines three `Protocol`s. They are the whole
extension story.

```python
class SearchProvider(Protocol):
    name: str
    requires_key: bool

    async def search(self, query: str, *, limit: int = 10) -> Sequence[SearchHit]: ...


class PageProvider(Protocol):
    name: str
    requires_key: bool

    async def get_text(self, url: str) -> str: ...


class LLMProvider(Protocol):
    name: str
    requires_key: bool

    async def plan_queries(self, request: PlanRequest) -> Sequence[str]: ...
    async def extract_fields(self, request: ExtractRequest) -> Sequence[ExtractedField]: ...
    def usage(self) -> LLMUsage: ...
```

The LLM contract is **task-shaped, not chat-shaped**. A provider is asked to
"plan queries" and "extract these fields", never to complete a prompt. That is
what lets `ExtractiveLLM` — a regex engine with no model behind it — satisfy the
same contract as a hosted model, and it is why the agent contains no
model-specific branching at all.

### Resolution ladder

`Registry` resolves `auto` to the best available provider, then keeps the rest
as fallbacks. Every hop is recorded in `Registry.notes`, which the CLI prints,
so a degraded run is visible rather than silent.

```
search:  brave (key)  →  brightdata-serp (key)  →  duckduckgo (always)
llm:     openai (key)  →  extractive (always)
```

## Data flow

`ResearchSpec` is a frozen value; the loop mutates only `Usage` counters and
local lists. Claims are derived, never accumulated, so reconciliation is
idempotent and re-running it over the same candidates gives the same report.

```
ResearchSpec ──▶ PlanRequest ──▶ [str]                    # stage 1
             ──▶ SearchHit    ──▶ [SearchHit]             # stage 2  (policy-filtered)
             ──▶ FetchResult  ──▶ PageContent            # stages 4-5
             ──▶ ExtractRequest ─▶ [ExtractedField]       # stage 6
             ──▶ FieldCandidate ─▶ [FieldCandidate]       # + structured data
             ──▶ [Claim] + unresolved                      # stage 7
             ──▶ ResearchReport                            # stage 8
```

## Key decisions

**Only `net/http.py` opens a socket.** Everything else consumes its results. One
place to audit for SSRF, TLS, retries and politeness.

**Redirects are re-validated.** A permitted URL can redirect to
`http://169.254.169.254/`; each hop runs the guard again, and so does robots
lookup for the new origin.

**Caching is conditional.** Fresh entries skip the network; stale ones revalidate
with `If-None-Match` / `If-Modified-Since` and a `304` refreshes the entry
rather than re-downloading it.

**Confidence is computed, not reported.** `verifier.reconcile` derives a Wilson
lower bound from observed agreement. An extractor cannot inflate its own score
past the `0.10` weight it is given.

**Evidence diversity beats evidence volume.** `_collect_evidence` keeps one
citation per domain before any second citation from the same domain, because ten
pages of one blog are one source wearing ten hats.

**Failures are values.** Every provider call is wrapped; a failure is logged and
the stack advances. `AutonomousScraperAgent.run` only re-raises
`asyncio.CancelledError`. A run that finds nothing still returns a report saying
so, with the notes explaining why.

## Threat model

| Threat | Control | Where |
| --- | --- | --- |
| SSRF to internal services | scheme allowlist, credential rejection, DNS resolution + public-IP check on every request **and redirect hop** | `net/guard.py` |
| DNS rebinding | host resolved and validated before the request; the answer is re-resolved with the cache bypassed immediately before the socket opens and must be a subset of the approved set, on every hop | `net/guard.py`, `net/http.py` |
| Prompt injection via page text | page text fenced in a delimited block, declared untrusted, model instructed never to obey it; output schema validated and every quote re-checked against the source | `providers/llm_openai.py` |
| Hallucinated values | quotes must appear verbatim in the page or the value is dropped | `providers/llm_openai.py` |
| Unbounded spend | six budgets, checked before every stage | `agent/loop.py` |
| Accidentally ignoring robots | enforced on every hop, including cross-origin redirects | `net/robots.py` |
| Secret leakage | redacting log filter; `Config.redacted()` masks credentials | `observability.py`, `config.py` |
| Malicious redirect loops | bounded `max_redirects`, then `FetchError` | `net/http.py` |
| Decompression bomb / huge page | `max_page_bytes` enforced from `Content-Length` *and* mid-stream | `net/http.py` |
| Malformed HTML | parser is lenient by construction; catches its own failures | `extract/html.py` |

### Known limitation: rebinding window

`SSRFGuard.recheck` narrows the DNS-rebinding window to the gap between its
lookup and the client's own, but it does not eliminate it. httpx resolves the
hostname itself, so a determined attacker controlling the authoritative DNS
record could in principle still win that last race. Closing it fully requires
connecting to a pre-validated IP while preserving the `Host` header and TLS SNI,
which is not expressible through httpx's public transport API. IP literals are
exempt from the re-check because they cannot be rebound.

## Test strategy

No mocking framework. `tests/conftest.py` starts a real `http.server` on
loopback and the SSRF guard is opened to private hosts only for those tests.
Real sockets mean redirects, `Retry-After`, robots parsing and size caps are
exercised for real.

The offline stack is not a stub — `ExtractiveLLM` is the default provider in CI
and under `--offline`, so every layer is covered without an API key.

`--offline` and `--no-network` are deliberately different. `--offline` selects
the keyless provider stack and still uses the network; `--no-network` opens no
socket at all and answers from cache. The latter is enforced in `net/client.py`,
which is the only place an `httpx.AsyncClient` is built outside tests. Providers
go through `build_async_client`, so a new provider inherits the guarantee by
default instead of having to remember to opt in, and `NetworkOffTransport`
refuses before a socket exists. Cached responses are served ahead of the robots
lookup, because serving from disk needs no network.

```
tests/
  test_models.py        value semantics, Wilson bound, dedupe
  test_config.py        env parsing, validation, redaction
  test_robots.py        RFC 9309 matching, specificity, crawl-delay
  test_guard.py         SSRF rejections, DNS-rebinding re-checks
  test_client.py        the network kill switch
  test_cache.py         freshness, revalidation, corruption tolerance
  test_extract.py       HTML parser, main-content, cue extraction
  test_providers.py     registry resolution, OpenAI parsing, injection containment
  test_verifier.py      reconciliation, conflicts, corroboration
  test_loop.py          end-to-end against the fixture server
  test_http.py          redirects, retries, robots, caching, size caps
  test_report.py        Markdown / JSON / plain rendering
  test_cli.py           argument handling, exit codes, rendering
```

## Deliberate non-goals

- **No JavaScript execution.** Requires a browser; the main-content extractor
  covers server-rendered pages, which is where factual prose lives. Listed as a
  future `PageProvider`.
- **No crawl-wide frontier.** The agent reads search results, not entire sites.
  Deep crawling is a different tool with a different legal surface.
- **No opaque scraping as a product.** There is no "scrape this site" command
  without a stated research purpose, because that is the shape of tool that gets
  used badly.
- **No model fine-tuning or training data collection.** Out of scope.
