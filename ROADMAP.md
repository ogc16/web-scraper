# Roadmap

This is a **decision record**, not a schedule. There are no dates or milestones
here, because a date on a single-maintainer project is a promise nobody can
audit. What it does instead is keep three categories of "we are not doing this"
from getting confused with each other:

| Section | Means | Someone should act on it |
| --- | --- | --- |
| [Open work](#1-open-work) | Genuinely unfinished, and worth finishing | Yes |
| [Deliberate non-goals](#3-deliberate-non-goals) | A decision was made and reasoned about | Only if the stated condition changes |
| [Environmental limits](#4-environmental-limits) | Not fixable by changing this code | No — work around it |

Until an item appears in §1, it is not a bug and does not belong in a bug report.
See [SUPPORT.md](SUPPORT.md).

---

## 1. Open work

### 1.1 Issue and pull request templates

**Status:** open, small, uncontroversial.
`CONTRIBUTING.md` now describes the contribution process and what CI enforces,
but there is no `.github/ISSUE_TEMPLATE/` or pull request template to route
people through it. A bug template asking for `awsa --version`, `awsa doctor`
output and the exact command would save a round trip on every report. A PR
template listing `make ci` and the doc-update expectations would do the same for
contributors.

---

## 2. Recently decided

- **DNS rebinding window closed at the socket.** `SSRFGuard.recheck` re-resolves
  before each socket opens, and `net/pinning.py` now makes that vetted address
  *the address dialled*, inside httpcore's network backend — so `Host` and TLS
  SNI still work and an unapproved host is refused rather than resolved. Costs:
  a proxy disables pinning (warned, not papered over), and the pool must be
  rebuilt because httpx does not expose `network_backend`, hence the pinned
  `httpcore` range and a loud `_assert_pool_contract` check. Details in
  [ARCHITECTURE.md](ARCHITECTURE.md). Closing this exposed a separate pre-existing
  gap: `robots.txt` was fetched with no guard pass and no approval at all, which
  is now covered by the same `_guard_and_approve` path.
- **No CLA.** Contributions land under the existing MIT Licence; no per-PR
  signature required. Reduces friction for a project whose contribution surface
  is small.
- **No service level.** [SLA.md](SLA.md) records that no availability commitment
  is offered. Revisit only if a hosted offering exists — see §3.5.
- **One User-Agent by default.** `AWSA_USER_AGENT_ROTATION` can rotate identities
  per request, but it is empty unless set, and the first request always uses the
  configured agent. Presenting several identities is how bot detection recognises
  automated traffic, so the honest single identity is the default and rotating is
  an explicit choice by the operator. The tool also identifies itself and
  respects `robots.txt`; that posture is the product.
- **pydantic at the LLM boundary, not on the internal models.** The eleven
  dataclasses in `models.py` are built by our own code and already validated in
  `__post_init__`; converting them would mean a compiled-core dependency, 45
  `dataclasses.replace` call sites, and no new validation. Model output is the one
  genuinely untrusted input, so the schema lives in `providers/schemas.py` and
  guards exactly that. This replaced `str(row.get("value", ""))`, which turned a
  model's `null` into the text `"None"` — which reads as an extracted value and can
  end up cited as evidence.
- **Strict JSON output.** `dump_json` raises `SerializationError` naming the
  offending path rather than falling back to `default=str`. A report that quietly
  coerces a malformed field is worse than one that fails, because the corruption
  surfaces later, in someone else's analysis, with no error to trace.

---

## 3. Deliberate non-goals

Each of these was a decision. The "reopen if" line is the part that matters: a
non-goal with a stated reopening condition is a decision, and a bare "no" is
just a refusal to think about it.

### 3.1 No JavaScript execution

The main-content extractor targets server-rendered pages, which is where factual
prose usually lives. Running a browser is a different class of tool with a
different dependency and operational surface.

**Reopen if:** client-rendered pages become a significant share of the sources
worth researching. The intended shape is a `PageProvider` — a browser-backed
implementation behind the existing Protocol — not a rewrite of the extractor.

### 3.2 No crawl-wide frontier

The agent reads search results, not entire sites. Deep crawling is a different
tool with a different legal surface and a much larger blast radius for a polite-
crawler guarantee that is only tested at the scale of a research run.

**Reopen if:** never on request alone. It would need its own rate limits,
budgets and legal review, not a flag on the existing agent.

### 3.3 No opaque "scrape this site" command

There is no command that scrapes a site without a stated research purpose,
because that is the shape of tool that ends up being used badly — and the
research framing is what makes the corroboration requirement coherent.

**Reopen if:** a use case arrives where the framing is real but the interface is
awkward. Fix the interface, not the principle.

### 3.4 No model fine-tuning or training data collection

Out of scope. The project reports what sources say; it does not build models.

### 3.5 No hosted service

There is nobody operating `awsa` on a user's behalf, which is why
[SLA.md](SLA.md) has no availability figure to state.

**Reopen if:** someone actually operates a service. At that point §1 is a
starting point for real operational work, and the SLA needs to be a signed
agreement with a legal entity — not a document edited in a pull request.

---

## 4. Environmental limits

These are not on the above list because no change to this code fixes them.

- **Keyless search is best-effort.** DuckDuckGo's anonymous endpoint rate-limits
  and bot-challenges unidentified clients. `awsa` reports this explicitly rather
  than returning zero results. **Workaround:** `AWSA_BRAVE_API_KEY`.
- **A fetched site can be down, slow, or blocking.** `awsa` surfaces each as a
  distinct outcome; it cannot prevent it. The `LOW` confidence tier and the
  `unresolved_fields` list exist so this is visible in the output.
- **CI runners can reach the cloud metadata address at `169.254.169.254`.**
  Observed while fixing `test_redirect_to_metadata_endpoint_is_blocked`, which
  had been passing locally only because nothing answered there. The test no
  longer depends on it, but the property is worth knowing about: do not write
  tests whose result depends on whether a well-known address happens to be
  reachable from the runner.

---

## Contributing to this file

Add to §1 when something is genuinely unfinished. Add to §3 when a decision is
made, and write the reopening condition at the time — it is much harder to
reconstruct later. If an item in §1 is finished, move it to §2 with a one-line
record of what changed, or delete it if the decision record in §3 already covers
the outcome.
