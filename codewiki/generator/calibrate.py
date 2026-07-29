"""
calibrate.py — summarizer prompt calibration on a small pinned sample (LLM; never touches the DB).

Before the multi-hour summary backfill, generate summaries for ~10 representative nodes into a
review directory, side-by-side with the exact context sent and the real source. A human (or a
review agent) checks each for factual accuracy, hallucinated symbols and whether it explains
*why* the node exists — then the prompt is refined, ``SUMMARY_PROMPT_VERSION`` bumped, and the
SAME sample re-run for a before/after comparison.

The sample is stratified (kinds × languages, incl. one async fn and one route handler) and
pinned in ``calibration_set.json`` so reruns — across prompt versions AND future model swaps —
always score the same nodes. Outputs land in ``calibration/<prompt_version>/``, one markdown
file per node. Nothing is written to the summaries table.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from codewiki.generator import context as ctx
from codewiki.generator import summarize as S
from codewiki.paths import STATE_DIR

CALIBRATION_SET = STATE_DIR / "calibration_set.json"
CALIBRATION_DIR = STATE_DIR / "calibration"

# (kind, language, extra SQL filter) — deterministic pick per stratum, mid-sized bodies preferred.
# A stratum with no matching rows in a given repo is just skipped (see run_calibration below),
# so this list can name languages/kinds a repo doesn't have without failing the run.
_STRATA: list[tuple[str, str, str]] = [
    ("function", "python", "s.signature LIKE 'async def%'"),
    ("method", "python", ""),
    ("function", "rust", ""),
    ("function", "javascript", ""),
    ("class", "python", ""),
    ("class", "rust", ""),
    ("module", "python", "s.end_line >= 40"),
    ("module", "javascript", ""),
]


def _pick(conn: sqlite3.Connection, kind: str, lang: str, extra: str) -> str | None:
    where = ["s.kind=?", "f.language=?"]
    if kind != "module":
        where.append("s.end_line - s.start_line BETWEEN 8 AND 120")
    if extra:
        where.append(extra)
    row = conn.execute(
        f"SELECT s.id FROM symbols s JOIN files f ON f.path=s.file_path "
        f"WHERE {' AND '.join(where)} ORDER BY s.id LIMIT 1", (kind, lang)).fetchone()
    return row["id"] if row else None


def build_set(conn: sqlite3.Connection, n: int = 10) -> list[str]:
    """Stratified node ids: kinds × languages + one route handler + one package rollup."""
    ids: list[str] = []
    for kind, lang, extra in _STRATA:
        nid = _pick(conn, kind, lang, extra) or _pick(conn, kind, lang, "")
        if nid and nid not in ids:
            ids.append(nid)
    row = conn.execute(  # a FastAPI route handler (decorated function)
        "SELECT id FROM symbols WHERE kind IN ('function','method') "
        "AND decorators LIKE '%router.%' ORDER BY id LIMIT 1").fetchone()
    if row and row["id"] not in ids:
        ids.append(row["id"])
    # a package rollup — prefer one whose modules already carry summaries (real context)
    row = conn.execute(
        "SELECT s.package, COUNT(DISTINCT s.file_path) n FROM symbols s "
        "JOIN summaries su ON su.node_id=s.id WHERE s.kind='module' "
        "GROUP BY s.package HAVING n >= 3 ORDER BY s.package LIMIT 1").fetchone()
    if row is None:
        row = conn.execute(
            "SELECT package, COUNT(DISTINCT file_path) n FROM symbols WHERE kind='module' "
            "GROUP BY package HAVING n >= 3 ORDER BY package LIMIT 1").fetchone()
    if row:
        ids.append(f"pkg::{row['package']}")
    return ids[:n]


def load_or_create_set(conn: sqlite3.Connection, n: int = 10, reset: bool = False) -> list[str]:
    if CALIBRATION_SET.exists() and not reset:
        data = json.loads(CALIBRATION_SET.read_text(encoding="utf-8"))
        return data.get("nodes", [])
    ids = build_set(conn, n)
    CALIBRATION_SET.parent.mkdir(parents=True, exist_ok=True)
    CALIBRATION_SET.write_text(json.dumps({
        "created_at": datetime.now(timezone.utc).isoformat(), "nodes": ids,
    }, indent=2) + "\n", encoding="utf-8")
    return ids


def _node_prompt(conn: sqlite3.Connection, node_id: str) -> tuple[str, str] | None:
    """(prompt, source_for_review) for a symbol or pkg:: node — same builders summarize uses."""
    if node_id.startswith("pkg::"):
        pkg = node_id[len("pkg::"):]
        mods = ctx.package_module_summaries(conn, pkg)
        if not mods:
            # pre-backfill DB: approximate the rollup context with module docstrings so the
            # prompt still gets exercised (real runs always have module summaries first)
            rows = conn.execute(
                "SELECT name, docstring FROM symbols WHERE kind='module' AND package=? "
                "ORDER BY file_path LIMIT 12", (pkg,))
            mods = [(r["name"], r["docstring"] or "(no docstring)") for r in rows]
        block = f"PACKAGE: {pkg}\nMODULES:\n" + "\n".join(f"  - {n}: {s}" for n, s in mods)
        return S._container_prompt("module", block), "(package rollup — see module summaries above)"
    sym = conn.execute("SELECT * FROM symbols WHERE id=?", (node_id,)).fetchone()
    if sym is None:
        return None
    src = ctx._read_span(sym["file_path"], sym["start_line"], sym["end_line"])
    if sym["kind"] in ("function", "method"):
        return S._leaf_prompt(ctx.leaf_context(conn, sym)), src
    return S._container_prompt(sym["kind"], ctx.container_context(conn, sym)), src


def run_calibration(conn: sqlite3.Connection, *, chat_fn=None, model: str = "",
                    n: int = 10, reset_set: bool = False,
                    out_dir: Path | None = None) -> dict:
    chat_fn = chat_fn or S.lmstudio_chat
    ids = load_or_create_set(conn, n, reset=reset_set)
    out = (out_dir or CALIBRATION_DIR) / S.SUMMARY_PROMPT_VERSION
    out.mkdir(parents=True, exist_ok=True)

    results = {"prompt_version": S.SUMMARY_PROMPT_VERSION, "model": model,
               "nodes": [], "failed": []}
    for i, node_id in enumerate(ids, 1):
        built = _node_prompt(conn, node_id)
        if built is None:
            results["failed"].append({"id": node_id, "error": "node no longer in graph"})
            continue
        prompt, src = built
        try:
            text, usage = chat_fn(prompt, model=model)
        except Exception as exc:
            results["failed"].append({"id": node_id, "error": str(exc)})
            continue
        summary = S._parse_json(text)
        slug = node_id.replace("/", "_").replace(":", "_")[:120]
        (out / f"{i:02d}_{slug}.md").write_text(
            f"# {node_id}\n\n"
            f"prompt_version: `{S.SUMMARY_PROMPT_VERSION}`  model: `{model}`  "
            f"tokens: {usage.get('prompt_tokens', 0)} in / {usage.get('completion_tokens', 0)} out\n\n"
            f"## Model output\n\n```json\n{json.dumps(summary, indent=2, ensure_ascii=False)}\n```\n\n"
            f"## Context sent\n\n````\n{prompt}\n````\n\n"
            f"## Source (ground truth for review)\n\n````\n{src}\n````\n",
            encoding="utf-8")
        results["nodes"].append({"id": node_id, "summary": summary,
                                 "tokens_out": usage.get("completion_tokens", 0)})
    (out / "_results.json").write_text(
        json.dumps(results, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return results
