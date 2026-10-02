# Security Policy

## Reporting a vulnerability

**Do not open a public issue, pull request, or discussion for a security
problem.** Public reports give an attacker a head start and can invalidate a
fix before it ships.

Use GitHub's private reporting, which is the channel for vulnerabilities in this
repository:

**[Report privately](https://github.com/ogc16/web-scraper/security/advisories/new)**

If that link is unavailable to you, email **82827770+ogc16@users.noreply.github.com**
with `SECURITY` in the subject line. Either way, you will get an acknowledgement
that the report was read.

Please include:

- Type of issue and the component or file involved
- Steps to reproduce, or a proof of concept
- The impact you believe it has
- Any suggested mitigation you already know of

You do not need to be certain. A plausible SSRF bypass or quote-verification
failure reported in good faith is worth more than a silent disclosure.

## What to expect

| Stage | Target |
| --- | --- |
| Acknowledgement | 3 days |
| Triage and severity assessment | 7 days |
| Fix or mitigation plan for confirmed, high-severity issues | 14 days |
| Public advisory | On fix, with credit unless you ask otherwise |

These are targets for a single-maintainer project, not guarantees. If a report
is going to sit longer than the window above, you will be told that rather than
left guessing.

## Supported versions

Only the newest released version receives security fixes. `0.x` releases are
not backported, except for a trivial fix that does not change behaviour for
users who are not affected.

| Version | Supported |
| --- | --- |
| `0.1.x` (latest) | Yes |
| Anything older | No |

## Threat model in one page

Knowing what the tool defends against is useful when deciding whether a report
is a real vulnerability or intended behaviour. Full detail is in
[ARCHITECTURE.md](ARCHITECTURE.md).

**`awsa` is assumed to be pointed at hostile input.** Page content, search
results, and provider responses are untrusted.

Defences that exist:

| Control | Where |
| --- | --- |
| Scheme, credential, and resolved-IP checks (SSRF guard) | `src/awsa/net/guard.py` |
| Re-validation immediately before each socket, including redirects | `src/awsa/net/http.py` |
| RFC 9309 robots.txt enforcement, on every hop | `src/awsa/net/robots.py` |
| Verbatim-quote re-check — a value that cannot be located in the fetched page is dropped | `src/awsa/providers/llm_openai.py` |
| Prompt-injection containment — page text is delimited and instructed never to be obeyed as instruction | `src/awsa/providers/llm_openai.py` |
| API-key redaction on the `awsa` logger | `src/awsa/observability.py` |
| Per-host pacing, jittered backoff, byte caps, wall-clock budget | `src/awsa/net/ratelimit.py`, `src/awsa/models.py` |

Deliberate, non-defences — these are documented behaviour, **not** vulnerabilities:

- **`AWSA_RESPECT_ROBOTS=false`** disables robots enforcement on request. It is
  a feature that removes a safety mechanism; the authorisation burden is the
  operator's.
- **`--allow-private-hosts`** permits private, loopback, and link-local
  destinations, which is what makes the SSRF guard stop working. It exists so
  the test suite can reach a local fixture server. Do not enable it against
  untrusted input.
- **DNS rebinding is closed for direct connections, and disabled for proxies.**
  The guard re-resolves immediately before the socket opens, and the validated
  address is then the address dialled (`net/pinning.py`), so the client never
  resolves the name for itself. A host with no approval is refused rather than
  resolved. When a proxy is configured the proxy performs the resolution, so
  pinning cannot apply; `PinningTransport` logs a warning and does not claim
  otherwise. Treat proxy use as outside this protection.
- **`httpcore` is a pinned dependency for this reason.** Pinning installs a
  network backend into httpcore's connection pool, which httpx does not expose.
  An httpcore release that changes the pool signature will raise at client
  construction rather than silently reverting to unpinned connections.
- **Scraping public pages can still be unlawful** depending on jurisdiction and
  the target's terms. That is an operator responsibility, not a code defect.

## Out of scope

- Vulnerabilities in upstream dependencies, unless `awsa` misuses them.
- Sites blocking or bot-challenging the client, including keyless search
  providers rate-limiting anonymous traffic.
- Legal or compliance questions.
- Findings from automated scanners with no demonstrated impact.
- Denial of service caused by deliberately exceeding your own configured
  budgets.
