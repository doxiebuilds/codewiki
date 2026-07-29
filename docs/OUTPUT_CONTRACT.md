# Output contract

Everything codewiki writes lives under the target repo's `docs/` directory:

```
docs/
  wiki/
    manifest.json
    <slug>.md            # one per page
  .codewiki_state/
    codegraph.db          # sqlite code-graph (internal; not part of the contract)
    refresh_status.json   # only present while/after a `codewiki update --status-file ...` run
```

Anything that consumes generated wiki output (a web UI, a static-site build, a search index)
should only depend on the shapes documented here — not on `codegraph.db`'s schema, which is
free to change between versions. `codewiki/server/reader.py` and `codewiki/server/app.py` in
this repo are a reference implementation of exactly this contract.

## `docs/wiki/manifest.json`

```jsonc
{
  "generated_at": "2026-01-01T00:00:00+00:00",   // ISO 8601, UTC
  "model": "qwen2.5-coder-14b",                   // or "codewiki-deterministic" for --no-llm
  "page_count": 6,
  "pages": [
    {
      "id": "01-overview",
      "slug": "overview",
      "title": "Overview",
      "order": 1,
      "summary": "One-sentence summary of the page.",
      "file": "overview.md",                      // relative to docs/wiki/
      "source_refs": ["backend/app.py", "..."],    // files this page draws on (capped list)
      "written_at": "2026-01-01T00:00:00+00:00"
    }
  ]
}
```

- `pages` is already sorted by the taxonomy's declared `order`; consumers may re-sort if needed
  but should not assume input order otherwise.
- A page is present in `pages` only if it matched at least one package in the code graph —
  `pages.yaml` entries with no matching code are silently omitted, not emitted empty.
- `model` reflects whichever mode last wrote a page: the LLM page writer's model name, or the
  literal string `"codewiki-deterministic"` for the Jinja fallback (`--no-llm` / no LLM reachable).

## `docs/wiki/<slug>.md`

Plain Markdown, `# <title>` as the first line, `##` sections, optional ` ```mermaid ` fenced
diagrams, and (LLM-writer pages) a `**Sources:**` bullet list per section — either plain
`path:line-line` bullets or, when `codewiki.toml` sets `github_blob_base`, Markdown links to
GitHub blob URLs. Treat the file as untrusted-origin Markdown for rendering purposes: sanitize
before injecting into a DOM (see `codewiki/viewer/index.html`'s `DOMPurify.sanitize` call) since
page prose is LLM-authored, even though citations are graph-verified.

## `docs/.codewiki_state/refresh_status.json`

Written only when `codewiki update` is invoked with `--status-file <path>` (see `progress.py`).
Absent entirely for plain `codewiki update` runs with no status file — a consumer should treat
a missing file the same as `{"state": "idle"}`.

```jsonc
{
  "state": "running",              // "running" | "done" | "error"
  "stage": "summarize",            // "index" | "summarize" | "pages"
  "pct": 42.5,                     // 0-100, stage-windowed: index 0-5, summarize 5-70, pages 70-100
  "detail": "120 stale nodes to summarize",
  "counts": {"done": 51, "total": 120},
  "run_id": "20260101T000000",
  "pid": 12345,
  "started_at": "2026-01-01T00:00:00+00:00",
  "finished_at": null,             // set on "done"/"error"
  "error": null                    // set on "error"
}
```

Writes are atomic (temp file + `os.replace`) so a poller never sees a torn snapshot. A consumer
polling this file should also independently verify `pid` is still alive (`os.kill(pid, 0)`) —
a crashed process leaves `state: "running"` behind; treat that combination as stale/failed rather
than trusting `state` alone. `codewiki/server/app.py`'s `/api/wiki/refresh/status` does exactly
this, surfacing it as an `is_running` boolean plus a `"stale"` state override.

## CLI ⇄ HTTP mapping

| CLI (`codewiki ...`)                          | Reference HTTP route (`codewiki/server/app.py`) |
|------------------------------------------------|----------------------------------------------|
| `update --status-file <path>` (background)      | `POST /api/wiki/refresh`                      |
| reads `refresh_status.json`                     | `GET /api/wiki/refresh/status`                |
| (SIGTERM the child)                             | `POST /api/wiki/refresh/stop`                 |
| reads `manifest.json`                           | `GET /api/wiki/manifest`                      |
| reads `<slug>.md`                               | `GET /api/wiki/page/{slug}`                   |
| substring search over `docs/wiki/*.md`          | `GET /api/wiki/search?q=...`                  |

Any consuming application is free to reimplement this mapping in its own stack (as the reference
FastAPI app does) — the only hard requirement is reading the files above according to this
contract.
