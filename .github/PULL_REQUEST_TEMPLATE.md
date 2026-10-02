## What does this change, and what problem does it solve?

<!--
One concern per PR. If the description needs the word "and", that is usually two
pull requests. Say the problem, not just the diff — a reviewer who knows why the
change exists catches things a reviewer looking at 400 changed lines cannot.

Closes #<!-- issue number, if any -->
-->

## How was this tested?

<!--
`make ci` is exactly what CI runs, and it passes locally before you push. If a gate
does not pass, fix it in the same PR: a red branch is not a reviewable state.

Beyond that: what would have caught this bug before? A test that fails on the old
code and passes on the new one is worth more than a description of the new code.
-->

- [ ] `make ci` passes locally
- [ ] New behaviour has a test that fails without this change
- [ ] Existing tests still pass (no assertions weakened to go green)

## Does this need a decision record?

- [ ] `README.md` updated — user-facing behaviour changed
- [ ] `ARCHITECTURE.md` updated — a decision or its rationale changed
- [ ] `ARCHITECTURE.md`'s test map updated — a test file was added
- [ ] `SECURITY.md` updated — a threat, control or caveat changed
- [ ] Nothing above applies

## Safety checklist

<!-- Leave "not applicable" ticked if the change cannot touch these. -->

- [ ] New network requests go through the SSRF guard, or are deliberately excluded and explained
- [ ] New code respects `robots.txt`, per-host pacing and the size caps
- [ ] New prompt text is fenced as untrusted, and model output stays schema-validated with quotes re-checked
- [ ] No new credentials, tokens or secrets are hardcoded, logged, or written to a report
- [ ] A new dependency is justified in the PR description, and pinned where a version change would change behaviour
- [ ] Not applicable — none of the above can apply to this change