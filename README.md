# codewiki

A wiki generator that reads your code instead of guessing at it. The idea is simple: parse the repo into a real symbol graph first, then let a local LLM write pages that can only cite things the graph actually contains.

This is a personal project, built to see how far grounding can push the quality of generated documentation.

Everything runs locally. No cloud calls, no API keys — it talks to any OpenAI-compatible server you're already running (LM Studio, Ollama, vLLM).

---

## 🤝 Contributing

Issues and pull requests are limited to collaborators. If you're not one, use Discussions to share feedback, suggestions, or bugs — I'll fold in what makes sense.

---

## ⚠️ Disclaimer

This is a documentation tool built solo for personal use, and the pages it writes are still LLM output. Grounding narrows the gap between "what the docs say" and "what the code does" — it doesn't close it.

**The summaries are generated, not verified.** Citations are checked against the code graph, so a `path:line` reference points at something real. Whether the prose *about* that code is correct is a separate question, and nothing here guarantees it. Read the output before you publish it.

**Quality tracks the model you point it at.** A small 3B model will produce 3B-quality prose. If the pages read thin, that's usually the model, not the pipeline — try a larger one before filing a bug.

**Language coverage is uneven.** Python, Rust, TypeScript, and JavaScript get a real symbol graph with call and import edges. Shell, YAML, TOML, JSON, and SQL are indexed at file level only, so pages about those parts of a repo will be shallower.

**It writes into your repo.** Output lands in `docs/wiki/` and `docs/.codewiki_state/` in whatever repo you run it against. Review the diff before committing, especially the first time.

**Early days.** This is `0.1.0`. The CLI surface and the output contract may shift between versions, and there's no PyPI release yet.

The contributor(s) take no liability for what you do with the generated documentation — inaccurate docs, misleading diagrams, whatever downstream decisions get made from them. Treat the output as a draft that needs review, not as a source of truth.

---

## How it works

A few stages stack together here:

1. **tree-sitter parses the repo** into a code graph — files, symbols, imports, call edges. No LLM involved, so it's reproducible and cheap enough to run on every push.
2. **Each symbol gets summarized** by your local model, and the summary is stored keyed by the content hash of the code it describes.
3. **Only changed code re-summarizes.** Edit one function and that function's branch regenerates; the rest of the repo is a cache hit.
4. **The page writer plans, drafts, then cites** — pulling `path:line` locations out of the graph rather than out of the model's memory.
5. **Every citation is validated** against the graph before the page is written. Bad ones get repaired or dropped, never silently kept.
6. **Mermaid diagrams are checked against real edges**, so the architecture picture reflects imports and calls that exist.

Put together, the deterministic stages do the work that has to be exact, and the model is only asked to write prose about facts it was handed. It can still be wrong about *meaning* — see the disclaimer — but it can't invent a function that isn't there.

---

## What it writes into your repo

codewiki installs once, on your machine, and runs *against* a target repo. Its own source never gets copied into the repo you're documenting. The only footprint is generated output:

| Path | What it is |
|------|-----------|
| `docs/wiki/*.md` | The generated wiki pages |
| `docs/wiki/manifest.json` | Page index and metadata |
| `docs/.codewiki_state/codegraph.db` | The code graph plus cached summaries |

That's it — no config folder, no dependency added to your project, nothing to import. If you want it gone, delete those two directories.

Vendored trees (`node_modules`, `.venv`, `venv`, `dist`, `build`, `target`, ...) are pruned during the walk, so you don't need to configure ignores to keep dependencies out of your wiki.

---

## Quick Start

### Prerequisites

- **Python 3.11+**
- **A local LLM server**: this defaults to [LM Studio](https://lmstudio.ai/) at `http://localhost:1234/v1`. Swap it out anytime — point it at Ollama, vLLM, or anything else that speaks the OpenAI chat API.
- **A git repository** to document. codewiki finds the repo root itself, so you can run it from any subdirectory.

### Install

From PyPI — the distribution is named `codegraph-wiki`, but the import and the CLI command are
both still `codewiki`:

```bash
pipx install codegraph-wiki
```

Or straight from the repo, adding the `server` extra if you want the bundled viewer:

```bash
pip install "codegraph-wiki[server] @ git+https://github.com/doxiebuilds/codewiki.git"
```

### Run it on a repo

1. Start your local LLM server and tell codewiki which model to use:
```bash
   export CODEWIKI_MODEL=your-model-name
   export CODEWIKI_LLM_BASE_URL=http://localhost:1234/v1   # only if you're not on the default
```
2. Change into the repo you want to document, build the code graph, and scaffold a page layout:
```bash
   cd /path/to/your/repo
   codewiki index --full   # build the graph (no LLM)
   codewiki init           # write a pages.yaml matching this repo's directories
```
3. Review the generated `pages.yaml` — rename pages, merge or split groups — then build the wiki:
```bash
   codewiki update
```

`update` runs indexing, summarization, and page writing in order and skips anything still fresh, so re-running it after a few commits only regenerates what actually changed.

The `init` step matters: page `include` prefixes have to match your real directory names. Skip it and codewiki falls back to a generic starter taxonomy that assumes `src/`, `app/`, `api/`, and friends — if your repo isn't laid out that way, nothing matches and you get zero pages.

If you'd rather drive the stages yourself:

```bash
codewiki init                # scaffold pages.yaml from the indexed graph
codewiki index --full        # build the code graph (no LLM)
codewiki summarize           # hash-gated summaries
codewiki assemble --writer   # write docs/wiki/*.md + manifest.json
codewiki status              # freshness + token totals
```

The indexing step works with no model loaded at all, so you can check what codewiki sees in your repo before committing to a full summarization run.

### View it

The generated Markdown is readable as-is on GitHub. For the bundled viewer:

```bash
uvicorn codewiki.server.app:app --reload &
python -c "import importlib.resources, webbrowser; \
  webbrowser.open(str(importlib.resources.files('codewiki') / 'viewer' / 'index.html'))"
```

---

## Configuring for your repo

Everything below is optional — codewiki runs with no config at all. Drop a `codewiki.toml` at your repo's root when you need to override the defaults (env vars always win; see `codewiki/config.py` for the full resolution order):

```toml
[codewiki]
source_subdir = ""                 # index only this subdir, if your code isn't at the repo root
github_blob_base = "https://github.com/you/repo/blob/main/"   # makes Sources links clickable

[[diagram.layers]]                 # architecture-diagram layer taxonomy
key = "api"
title = "API"
css_class = "api"
needles = ["apps/api", "server/"]
```

`github_blob_base` is derived from your `origin` remote when it points at GitHub, so most repos won't need to set it.

The page taxonomy lives in `pages.yaml` at the repo root. `codewiki init` generates one from your indexed graph, which is easier than writing it by hand — but the format is small enough to edit directly:

```yaml
pages:
  - slug: overview
    title: Overview
    order: 1
    include: [src, app]        # package path prefixes
    domain: [route, env_flag]  # optional reference tables
```

`include` prefixes are matched against the package paths in the code graph, so they have to reflect real directories. Without a `pages.yaml`, codewiki falls back to the generic starter at `codewiki/assembly/pages.example.yaml`; if that matches nothing, `assemble` tells you so and points you at `codewiki init`.

---

## Writing your own domain extractor

Beyond generic code structure, codewiki pulls typed reference tables straight out of the code. Seven extractors ship built-in — HTTP routes, DB tables, pub/sub channels, websocket events, Rust FFI exports, frontend API calls, and env flags (see `codewiki/indexer/domain/builtin.py`).

For anything specific to your repo — a service registry, a feature-flag system — register your own:

```python
from codewiki.indexer.domain import register

def extract_my_thing(conn):
    ...  # return a list of {"id", "kind", "name", "detail", "file_path", "line"} rows

register("my_thing", extract_my_thing)
```

See `examples/plugins/services_tiers.py` for a worked example.

---

## Repository layout

```
codewiki/            the generator (CLI: `codewiki index|summarize|calibrate|assemble|update|status`)
  indexer/            tree-sitter code graph + pluggable domain extraction
  generator/          hash-gated LLM summaries
  writer/             the multi-step LLM page writer (plan -> sections -> diagrams -> validate)
  assembly/           deterministic page rendering + page taxonomy
  server/             reference FastAPI serving layer (reads docs/wiki/*, launches refreshes)
  viewer/             a single self-contained static HTML viewer
examples/plugins/     worked example of a custom domain extractor
tests/                pytest suite
```

---

## Documentation

- [Output Contract](docs/OUTPUT_CONTRACT.md) — the `manifest.json` / `<slug>.md` / `refresh_status.json` shapes
- [Contributing](CONTRIBUTING.md) — local setup, where things live, adding a language or extractor

Running the tests:

```bash
pip install -e ".[dev]"
pytest
```

---

## License

MIT — see [LICENSE](LICENSE).
