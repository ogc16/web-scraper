# Service Level Agreement

**No service level is offered, and none is in force.** This document records
that fact and explains what stands in its place. It contains no targets,
because there is no service for a target to describe.

## 1. What `awsa` is, and what that implies

`awsa` is a **Python library and command-line tool that you install and run
yourself**, distributed under the MIT Licence as source and as prebuilt wheels.

Nobody operates it on your behalf. There is no hosted API, no control plane, and
no endpoint we are responsible for keeping reachable. It follows that:

- There is no service whose availability could be measured, and therefore no
  availability figure that could be reported honestly. Any percentage stated
  here would be a number about nothing.
- The uptime of `awsa` in your deployment is the uptime of **your**
  infrastructure, and is governed by whatever agreement you have with that
  provider.
- A page fetch can fail for reasons entirely outside this project: the site may
  be down, robots.txt may disallow the path, your network path may break, or a
  search provider may rate-limit the client. `awsa` reports each of these as a
  distinct, inspectable outcome rather than hiding it, but reporting a failure
  is not preventing it.

## 2. Support

Support is **best-effort and community-run**. There are no response-time
targets, no severity matrix, and no uptime commitment, because there is nobody
obliged to be on call.

| | |
| --- | --- |
| Channel | [GitHub Issues](https://github.com/ogc16/WebScraper/issues) |
| Hours | None. Responses arrive when the maintainer is available. |
| P1 / P2 / P3 targets | None |

Commercial support is not offered and is not implied by opening an issue. The
honest answer to a support-contract request is to decline it rather than
promise something a single-maintainer project cannot reliably keep.

See [SUPPORT.md](SUPPORT.md) for how to file a useful report and for the
documented limitations that are not defects.

## 3. What is actually committed

Two things carry a real obligation, and both are elsewhere in the repository:

1. **Security disclosures.** Reports made through the private channel in
   [SECURITY.md](SECURITY.md) are acknowledged within 3 days and triaged within
   7. These are targets for a single-maintainer project, not guarantees — but
   they are the commitment being made, rather than one about uptime.
2. **Licence terms.** The MIT Licence in [LICENSE](LICENSE) governs, including
   its warranty disclaimer. The software is provided **"AS IS", WITHOUT WARRANTY
   OF ANY KIND**, express or implied. It is not fit for any particular purpose,
   and no claim of fitness for a particular use is made or implied anywhere in
   this repository.

## 4. Would a hosted offering change this?

Yes, and it would need a separate, signed agreement rather than an edit to this
file. A genuine service level requires facts that only an operator of a running
service can supply and be held to: a measured availability target, a monitoring
system independent of the application, defined exclusions, credit terms, and a
legal entity on the other side of the contract.

The `/` in that last point is the current blocker. Until someone is actually
operating something, any such document would be a template, not an agreement.

A previous draft of this file was such a template, with placeholder targets. It
was removed rather than left in place: an empty form in a contract-shaped file
tends to get filled in by someone who has not thought about whether the promise
is one they can keep.

## 5. Version

| | |
| --- | --- |
| Applies to | `awsa` as distributed, all versions in the `0.x` series |
| Last reviewed | 2026-09-28, at `v0.1.0` |
| Status | In force, as a statement that no service level is offered |
| Changes | Made by pull request. No notice period is meaningful for a document that promises nothing, but significant changes are still worth calling out in release notes. |
