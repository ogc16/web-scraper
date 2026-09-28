# Credits and provenance

This repository is the successor to two earlier projects by the same author.
Both were preserved as git remotes (`legacy-agent`, `legacy-scraper`) so the
history is traceable; their code is **not** carried forward, because neither
could run.

| Upstream | Remote | What it was |
| --- | --- | --- |
| `ogc16/web-scraping-agent-demo` | `legacy-agent` | OpenAI Agents SDK driving Bright Data's MCP server |
| `ogc16/web-scraper` | `legacy-scraper` | A ten-line `requests` script |

## Why they were replaced rather than merged

**`web-scraping-agent-demo`** was a single hardcoded example:

- One hardcoded subject, `"Tom Shaw Developer"`, passed straight to
  `Runner.run()`.
- Bright Data credentials written into `main.py` as literal
  `<YOUR API TOKEN HERE>` placeholders, while `.env.example` listed only
  `OPENAI_API_KEY` — the two disagreed about where secrets lived.
- Bright Data was mandatory, so the project could not be run or evaluated
  without a paid account, a Web Unlocker zone, and Scraping Browser credentials.
- No robots.txt handling, no rate limiting, no caching, no retries, no
  confidence model, and no test suite.
- The agent was an LLM with a scraper attached, not a system: nothing
  reconciled what it found, so a hallucinated value looked identical to a
  verified one.

**`web-scraper`** was, in full:

```python
import requests

result = requests.get("website url")
user = result.json()
```

`"website url"` is a literal string, not a variable, so the script raises
`MissingSchema` on the first run. It was a sketch, not code.

## What was kept

The intent, not the implementation. Specifically:

- **Bright Data integration**, reimplemented as two optional providers behind a
  neutral `PageProvider` / `SearchProvider` interface
  (`src/awsa/providers/brightdata.py`). Nothing imports it without credentials.
- **MCP-style extensibility**, generalised into a provider registry so search,
  fetching and language modelling are each independently replaceable.
- **The Agents-SDK loop shape** — plan, act, observe — kept, and given budgets,
  a stopping condition, and a reconciliation stage.

## What is new

Everything else. The merged project adds the parts that make a scraper
trustworthy: SSRF guarding, RFC 9309 robots enforcement, per-host pacing,
conditional-request caching, main-content extraction, cross-source
reconciliation with a Wilson confidence bound, verbatim-quote provenance, prompt
injection containment, six resource budgets, a CLI, and a hermetic test suite
that runs without network access.

The original code remains reachable in history:

```console
git show legacy-agent/main:main.py
git show legacy-scraper/main:main.py
git log --oneline --all
```
