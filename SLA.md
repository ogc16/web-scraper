# Service Level Agreement

> **This is a template, not an agreement.** Every `[PLACEHOLDER]` below is a
> decision only you can make and only you can commit to. Nothing here is
> currently in force, and publishing this file does not create an obligation.
> Have it reviewed by a lawyer before it governs anything.
>
> The placeholders are deliberate. A plausible-looking uptime figure in a
> document like this is not a placeholder — it reads as a promise, and someone
> will hold you to it.

## 1. Parties and scope

| | |
| --- | --- |
| Provider | `[PLACEHOLDER: legal entity name]`, `[PLACEHOLDER: address]` |
| Customer | `[PLACEHOLDER: customer legal entity]` |
| Effective date | `[PLACEHOLDER: YYYY-MM-DD]` |
| Applies to | `[PLACEHOLDER: which offering — e.g. the hosted awsa API, or this repository's release artifacts]` |
| Governing law | `[PLACEHOLDER: jurisdiction]` |

## 2. The default position: you run it yourself

This is the only position that applies unless you have a separate, signed
agreement with a party who actually operates something.

`awsa` is distributed as source and as prebuilt wheels. **No party operates it on
your behalf.** That has direct consequences for every clause below:

- There is no service to monitor, so there is no availability figure to report.
- Your deployment's uptime is your infrastructure's uptime.
- Outbound fetches depend on third-party sites being up, on robots.txt permitting
  the request, and on your network path. `awsa` surfaces each of these as a
  distinct outcome — it cannot make them reliable.
- A search provider rate-limiting or bot-challenging the client is a third-party
  behaviour, not a service failure.

Any SLA that purports to guarantee availability for software you run yourself is
describing something that does not exist.

## 3. Availability commitment

**Applies only if a party operates a hosted offering.** Delete this section
entirely if you do not have one.

| Metric | Value |
| --- | --- |
| Availability target | `[PLACEHOLDER: e.g. 99.5%]` |
| Measurement window | `[PLACEHOLDER: e.g. calendar month]` |
| Measurement method | `[PLACEHOLDER: how it is measured, by whom, from which vantage points]` |
| Scheduled maintenance | `[PLACEHOLDER: window, notice period, whether it counts against the target]` |
| Target excludes | `[PLACEHOLDER: customer misconfiguration, third-party provider limits, force majeure]` |

**Definitions that must be fixed before this is enforceable:**

- *Unavailable* means `[PLACEHOLDER: the observable condition — e.g. no successful
  response from any regional probe for N consecutive minutes]`. Vague wording here
  is the most common reason an SLA is unenforceable.
- *Request* means `[PLACEHOLDER: a well-formed call within documented rate
  limits]`. A customer exceeding rate limits must be excluded or the target is
  unachievable.
- *Error budget* means `[PLACEHOLDER: the credit formula]`.

## 4. Service credits

**Applies only to a hosted offering.** There are no credits for software
distributed under its licence.

| Monthly availability | Credit |
| --- | --- |
| `[PLACEHOLDER: below target]` | `[PLACEHOLDER: % of monthly fee]` |
| `[PLACEHOLDER]` | `[PLACEHOLDER]` |

A credit is the customer's sole remedy `[PLACEHOLDER: unless you also offer
termination rights — decide which, deliberately]`. Choose this explicitly: sole
remedy is simpler to operate, termination rights are stronger for the customer.

## 5. Support

Applies to both scopes, with different meaning.

| | Self-hosted | Hosted offering |
| --- | --- | --- |
| Channel | GitHub Issues | `[PLACEHOLDER: channel]` |
| Hours | Best-effort, community | `[PLACEHOLDER: e.g. 09:00–18:00 UTC, Mon–Fri]` |
| P1 response target | None | `[PLACEHOLDER: e.g. 1 hour]` |
| P2 response target | None | `[PLACEHOLDER: e.g. 1 business day]` |
| P3 response target | None | `[PLACEHOLDER: e.g. 3 business days]` |

For the self-hosted case see [SUPPORT.md](SUPPORT.md), which states the
best-effort position in full. Response targets are targets, not guarantees, unless
this document says otherwise and is signed.

## 6. Security incidents

For vulnerabilities in the software, see [SECURITY.md](SECURITY.md) — reports are
handled through private disclosure, and the disclosure timeline there applies
irrespective of any commercial agreement.

`[PLACEHOLDER: if a hosted offering is added, state the breach notification
window here, e.g. "confirmed breaches affecting customer data are notified within
72 hours of confirmation", and confirm it matches your actual regulatory
obligations. Do not copy a number from this template without checking which
regime you are in — GDPR Art. 33, for example, requires notification to the
supervisory authority within 72 hours of *awareness*, not confirmation.]`

## 7. Exclusions

An SLA without exclusions is not an SLA. At minimum, exclude:

- Periods where a third-party provider — search, model, or a fetched site — is
  unavailable, rate-limiting, or bot-challenging the client
- Customer misconfiguration, including disabled robots enforcement, exceeded
  budgets, or missing credentials
- Public internet, DNS, or certificate-authority failures
- Force majeure
- Beta or preview features, if you ship any `[PLACEHOLDER: name them]`

## 8. Changes and termination

- This document may be revised `[PLACEHOLDER: notice period]` before it takes
  effect.
- Either party may terminate for material breach after `[PLACEHOLDER: notice and
  cure period]`.
- On termination `[PLACEHOLDER: what happens to data, credentials, and any
  outstanding credits]`.

---

## Before you publish this

- [ ] Every `[PLACEHOLDER]` is replaced or the section is deleted
- [ ] The scope in §1 matches reality — someone actually operates the offering
- [ ] §2 is removed or kept deliberately; it contradicts §3 if kept by accident
- [ ] A lawyer has read it
- [ ] The monitoring that produces the §3 number actually exists and is queried
      independently of the provider
