# Support

## There is no SLA, and there will not be one

`awsa` is a **library and CLI that you install and run yourself**. Nobody hosts
it on your behalf, so there is no service whose uptime could be measured or
promised, and no party standing behind a response time. If you need a contractual
availability commitment, `awsa` is the wrong dependency for that requirement.

What that means in practice:

- You run the code. Its availability is your infrastructure's availability.
- When `awsa` fetches a page, the request depends on that page being up, on
  robots.txt permitting it, and on your network path. `awsa` reports each of
  those as a distinct, inspectable outcome rather than a silent failure.
- Support is **best-effort and community-run**. No response time is guaranteed.

## Where to ask

| I want to… | Go to |
| --- | --- |
| Report a bug | [Issues](https://github.com/ogc16/web-scraper/issues) |
| Ask how to use something | [Issues](https://github.com/ogc16/web-scraper/issues) or [Discussions](https://github.com/ogc16/web-scraper/discussions) |
| Report a **security** issue | **Do not open a public issue.** See [SECURITY.md](SECURITY.md). |
| Propose a change | Read [CONTRIBUTING.md](CONTRIBUTING.md), then open a pull request |

Before filing a bug, please check that it is not one of the known limitations
below — they are documented behaviour, not defects.

## Reporting a bug well

Include, at minimum:

1. `awsa --version` and your Python version (`awsa doctor` prints both).
2. The exact command you ran.
3. What you expected and what happened instead.
4. The relevant log lines at `AWSA_LOG_LEVEL=DEBUG`.

`awsa` redacts anything shaped like an API key before it reaches a log handler,
and `awsa providers` prints `sk-1…(51 chars)` rather than a value, so debug
output is normally safe to paste. **Check your own environment first** — a real
credential in a pasted URL, header dump, or `.env` is still your
responsibility. If you do paste a key, revoke and rotate it immediately; treat
it as compromised.

## Known limitations

These are properties of the design, listed so they are not mistaken for bugs.

- **Keyless search is best-effort.** DuckDuckGo's anonymous endpoint rate-limits
  and bot-challenges unidentified clients. When that happens `awsa` says so
  explicitly and points at the fix rather than reporting zero results. Set
  `AWSA_BRAVE_API_KEY` for unattended use.
- **DNS rebinding is mitigated, not eliminated.** The SSRF guard re-resolves a
  hostname and discards private answers *immediately before* a socket is
  opened, but `httpx` performs its own resolution when it dials. The window
  between the check and the connect is not closed by a true connection-level
  pin. Treat this as defence in depth, not as a complete rebinding defence.
- **`AWSA_RESPECT_ROBOTS=false` disables a safety feature.** Authorisation for
  whatever you then scrape is entirely your responsibility.
- **Extraction quality is bounded by the source.** A well-quoted value from one
  page is still one source. Read the `Corroborated` column before you rely on a
  value; the `LOW` confidence tier exists for exactly this reason.

## Stability

The project is **pre-1.0 (`0.x`)**. It follows semantic versioning in the sense
that breaking changes are concentrated in `0.x` minors, but the `0.x` series
carries **no** forward-compatibility promise: minors may break. Pin an exact
version if that matters to you, and read the release notes before upgrading.

Security fixes are applied to the latest release. Older releases are not
backported unless the fix is trivial and the fix would not change behaviour for
anyone not affected by the vulnerability.

## Commercial support

None is offered, and none is implied by opening an issue. This is a
single-maintainer open-source project; the honest answer to a support SLA
request is to say no rather than invent a commitment.
