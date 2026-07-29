# Contributing

Issues and pull requests are limited to collaborators. If you're not one, use Discussions to
share feedback, suggestions, or bugs — I'll fold in what makes sense. The notes below are for
anyone working in the codebase, collaborator or fork.

## Setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev,server]"
pytest
```

## Where things live

- `codewiki/indexer/` — tree-sitter parsing + the code-graph builder. No LLM calls here; this
  layer must stay deterministic and testable without a live model.
- `codewiki/generator/` — the one LLM stage that produces hash-gated node summaries.
- `codewiki/writer/` — the multi-step page writer (plan → sections → diagrams → validate). Prompt
  text lives in `writer/prompts.py`; bump `WRITER_PROMPT_VERSION` after any prompt change so
  already-written pages regenerate.
- `codewiki/assembly/` — deterministic page rendering, the page taxonomy loader, and the
  architecture-diagram generator.
- `codewiki/server/` + `codewiki/viewer/` — the reference serving layer. Keep it a thin
  consumer of `docs/OUTPUT_CONTRACT.md`, not a place for new generator logic.

## Adding a language

Add a parser under `codewiki/indexer/parsers/` implementing the same interface as
`parsers/python.py`, register its file extensions in `codewiki/paths.py`
(`INCLUDE_EXTS`/`LANG_BY_EXT`/`PARSED_LANGS`), and add fixtures under `tests/`.

## Adding a domain extractor

See the README's "Writing your own domain extractor" section and
`examples/plugins/services_tiers.py`. Built-in extractors in `codewiki/indexer/domain/builtin.py`
should stay generic (no assumptions about any one repo's directory layout or config format) —
anything repo-specific belongs in a separate, opt-in plugin.

## Publishing the wiki

`docs/wiki/` is mirrored into this repo's GitHub wiki by `scripts/publish_wiki.py`, which renames
pages to the wiki's flat namespace (first page becomes `Home`), rewrites `[x](page.md)` links to
`[x](Page)`, and generates `_Sidebar.md`/`_Footer.md`. Build locally without pushing:

```bash
python scripts/publish_wiki.py            # writes build/wiki/
python scripts/publish_wiki.py --push     # mirrors it to the wiki repo
```

The wiki is a downstream copy and is replaced wholesale on every sync — review page changes in
the PR that touches `docs/wiki/`, never in the wiki's web editor. `.github/workflows/publish-wiki.yml`
runs the sync on pushes to `main`. It needs two one-time setup steps: enable Settings → Features →
Wikis and save a first page in the web UI (GitHub doesn't create the wiki repo until then), and
add a PAT with `repo` scope as the `WIKI_TOKEN` secret (the default `GITHUB_TOKEN` can't push to
wikis).

## Tests

`pytest` from the repo root. Tests use a fixed synthetic config (see `tests/conftest.py`'s
`_fixed_config` fixture) so they're independent of whatever repo/remote they happen to run in.

## Changes

Keep them focused; include a test for behavior changes. If you touch prompt text in
`writer/prompts.py`, bump `WRITER_PROMPT_VERSION` (or `SUMMARY_PROMPT_VERSION` in
`generator/summarize.py` for summarizer prompt changes) in the same PR.
