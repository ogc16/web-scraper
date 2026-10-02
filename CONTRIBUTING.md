# Contributing to awsa

Thanks for considering a contribution. This document is short on ceremony and
specific on mechanics — the conventions below are enforced by CI, so reading
them first is faster than reading a CI failure.

By participating you agree to abide by the [Code of Conduct](CODE_OF_CONDUCT.md).

There is no contributor license agreement. Contributions land under the same
[MIT Licence](LICENSE) as the rest of the project, and no signature is needed.

If you want something to pick up, [ROADMAP.md](ROADMAP.md) §1 lists what is
genuinely unfinished. §3 of that file records what has already been decided
against, and why — read it before proposing a change that duplicates a decision
someone already made.

## Quick start

Requires Python 3.11+.

```console
git clone https://github.com/ogc16/WebScraper
cd WebScraper
python -m venv .venv
.venv/Scripts/python -m pip install -e ".[dev]"   # Windows
.venv/bin/python   -m pip install -e ".[dev]"   # macOS / Linux
make ci
```

`make ci` is exactly what CI runs. If it passes locally it will pass in the
workflow.

On Windows, run the tools through the venv interpreter
(`.venv\Scripts\python.exe -m pytest`) or `make`, rather than bare `pytest` /
`ruff`, so you test the same interpreter CI does.

## Everyday commands

| Command | What it does |
| --- | --- |
| `make dev` | Editable install with dev extras |
| `make test` | `pytest` — hermetic, no network required |
| `make lint` | `ruff check` + `ruff format --check` |
| `make format` | `ruff --fix` + `ruff format` |
| `make typecheck` | `mypy`, strict, over `src` **and** `tests` |
| `make check` | lint + typecheck + test |
| `make ci` | alias for `check` |
| `make build` | sdist + wheel into `dist/` |
| `make verify-dist` | install the built wheel in a throwaway venv and run it |
| `make clean` | remove build and cache artifacts |

## What CI actually enforces

`ci.yml` runs two jobs. The first gates on Python 3.11, 3.12 and 3.13; all three
must pass. The second job then builds the distributions and **installs the built
wheel into a clean venv and runs it**, so a packaging mistake cannot hide behind
an editable install.

Three gates, all of which run on your machine as `make ci`:

- **Ruff** — lint plus formatting check, including the `S` (bandit) and `D`
  (pydocstyle, Google convention) rule sets. Formatting is not negotiable;
  `make format` fixes it.
- **mypy, `strict = true`** — over `src/awsa` and `tests`. Strict means
  untyped defs are rejected, so a new function needs annotations.
- **pytest** — with `filterwarnings = ["error"]`, so **a warning fails the
  build**. Do not silence a warning you have not understood.

## Code conventions

- **Google-style docstrings** for modules, classes and public functions. Explain
  *why*, not what the next line does. Some `D` rules are disabled on purpose
  (methods, dunders, `__init__` are covered by the class docstring) — see the
  comments in `pyproject.toml` before adding more.
- **`Any` is restricted.** It is permitted in exactly two shapes: `**kwargs`
  forwarded into frozen dataclass constructors, and `**detail` structured
  logging fields. Return types stay concrete. A new `Any` needs a reason.
- **Line length 100**, enforced by ruff.
- **Comments explain intent and trade-offs.** The codebase leans on them; a
  comment restating the code is noise.

## Testing

This is the part most worth reading carefully.

**There is no mocking framework, and adding one is a regression in intent.**
`tests/conftest.py` starts a real `http.server` on loopback, and the SSRF guard
is opened to private hosts *only* for those tests. Real sockets mean redirects,
`Retry-After`, robots parsing, byte caps and retries are genuinely exercised
rather than asserted against a stand-in.

Rules that follow from that:

1. **Tests must not depend on ambient network reachability.** A test that passes
   because a connection to some address fails on *your* machine, but behaves
   differently where the address answers, is a broken test. It happened in this
   repository: the redirect-to-metadata test passed on Windows because nothing
   answered at `169.254.169.254`, and failed on CI runners, where it does answer.
   Assert on a control the test itself configures.
2. **The suite must run with no network access at all.** If a new test needs the
   internet, the design is wrong — add a fixture route in `conftest.py` instead.
3. **The keyless stack is the default in CI.** `ExtractiveLLM` is a real
   provider, not a stub, so no test may require an API key. If you add a
   provider, cover its registry resolution *and* its keyless degradation.
4. **`allow_private_hosts=True` in the test config is a concession, not a
   default.** If you add a test that relies on it, say in a comment which
   control is consequently inert.

### Adding a new test file

`tests/` has no `__init__.py`, so mypy sees each file as a top-level module
named `test_foo`. A `tests.*` pattern matches nothing, and mypy rejects a bare
`test_*`. **Add the module name to the `[[tool.mypy.overrides]]` list in
`pyproject.toml`** or the typecheck gate fails:

```toml
[[tool.mypy.overrides]]
module = [
  "conftest",
  "test_cache",
  # ... existing ...
  "test_your_new_file",
]
```

### Layout

`ARCHITECTURE.md` has the per-file map and the reasoning behind the layering.
The short version: `net/` is transport and safety, `extract/` is HTML to text,
`providers/` is the three Protocol implementations, `agent/` is the loop, spec
and reconciliation, `report.py` renders. Dependencies point downward; `net/`
does not know the agent exists.

## Adding a provider

The agent depends on three Protocols in `awsa.providers.base` — `SearchProvider`,
`PageProvider`, `LLMProvider` — and never on a concrete vendor. Implement the
relevant one, register it in `awsa.providers.registry`, and expose:

- `name` — the key used in config and shown by `awsa providers`
- `requires_key` — whether it can be used with no credentials

Then, if it needs HTTP, build its client through `build_async_client` from
`awsa.net.client` rather than `httpx.AsyncClient` directly. That is the single
place `--no-network` is enforced, so going through it means a new provider
inherits the guarantee instead of having to remember to opt in.

Test both paths: resolution from config, and graceful degradation when the key is
absent. A provider that aborts the run when unavailable is a bug — the registry
falls back.

## Security-sensitive changes

Read [SECURITY.md](SECURITY.md) before touching `net/guard.py`, `net/http.py`,
`net/robots.py`, the redaction filter in `observability.py`, or the quote
re-check in `providers/llm_openai.py`. Those are the controls the threat model
in `ARCHITECTURE.md` depends on.

Specifically:

- Do not weaken or bypass the SSRF guard, the robots check, or the pre-socket
  re-validation to make a test pass. Change the test.
- Do not log credentials, and do not remove the redacting filter.
- Never paste a real key into a test, a fixture, a doc, or a commit. The suite
  must stay runnable with no credentials configured.
- Page content is untrusted. New prompt text goes in a delimited block, and
  output stays schema-validated with quotes re-checked against the page.

## Commits and pull requests

Commit subjects follow the style already in the history:

```
feat: re-plan when fields stay uncorroborated
docs: add UML diagrams to README
test: make the redirect SSRF check independent of ambient reachability
refactor: relocate web-scraper files to src/legacy/web_scraper
```

Conventional prefixes (`feat`, `fix`, `docs`, `test`, `refactor`, `chore`),
imperative mood, one logical change per commit. A commit that needs "and" in its
message should probably be two commits.

`.github/PULL_REQUEST_TEMPLATE.md` asks for these in order, so most of them arrive
pre-filled with the question rather than needing to be remembered:

- One concern per PR. Drive-by refactors in a feature PR make review harder than
  the change itself.
- Say what problem you are solving, not just what you changed. If it fixes an
  issue, reference it.
- Confirm `make ci` passes. If CI fails, fix it in the same PR — a red branch is
  not a reviewable state.
- Update `README.md` if you changed user-facing behaviour, and `ARCHITECTURE.md`
  if you changed a decision or its rationale. Those two files are the project's
  memory; a change that makes them wrong is incomplete.
- Update `ARCHITECTURE.md`'s test map when you add a test file.

## Releasing

Version lives in **two** places, and they must agree:

- `version` in `pyproject.toml`
- `__version__` in `src/awsa/__init__.py`

A test enforces that they match, so a mismatched bump fails CI rather than
shipping a wheel whose metadata disagrees with `awsa --version`.

To cut a release:

1. Bump both files.
2. `make ci && make build && make verify-dist`.
3. Commit: `chore: release vX.Y.Z`.
4. Tag and push: `git tag -a vX.Y.Z -m "vX.Y.Z" && git push origin main --tags`.
5. Publish the GitHub release with `dist/*.whl` and `dist/*.tar.gz` attached, so
   the release is installable without a build step.

`dist/` is gitignored, so artifacts are always built locally and uploaded
explicitly — they are never committed.

## Licence

Contributions are accepted under the [MIT Licence](LICENSE), the licence of the
existing project.
