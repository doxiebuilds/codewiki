"""
db.py — thin sqlite3 wrapper for the code-graph store (no ORM, no LLM).

Opens/initialises ``codegraph.db`` from ``schema.sql`` and offers small typed helpers the
indexer, summarizer and assembler share. Kept deliberately boring: dict-row access, explicit
transactions, deterministic upserts.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Iterable

from codewiki.paths import GRAPH_DB, STATE_DIR

SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"


def connect(db_path: Path | str = GRAPH_DB) -> sqlite3.Connection:
    """Open (creating if needed) the code-graph DB with the schema applied."""
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    _migrate(conn)
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """Additive column migrations (CREATE TABLE IF NOT EXISTS can't alter existing tables)."""
    edge_cols = {r["name"] for r in conn.execute("PRAGMA table_info(edges)")}
    if "resolved" not in edge_cols:
        conn.execute("ALTER TABLE edges ADD COLUMN resolved TEXT NOT NULL DEFAULT ''")
        conn.commit()
    pb_cols = {r["name"] for r in conn.execute("PRAGMA table_info(page_builds)")}
    if pb_cols and "skeleton_json" not in pb_cols:
        conn.execute("ALTER TABLE page_builds ADD COLUMN skeleton_json TEXT NOT NULL DEFAULT '{}'")
        conn.commit()


# ------------------------------------------------------------------ meta
def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )


def get_meta(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


# ------------------------------------------------------------------ files / symbols upsert
def replace_file(conn: sqlite3.Connection, file_row: dict[str, Any], symbols: list[dict[str, Any]],
                 edges: Iterable[dict[str, Any]]) -> None:
    """Replace a single file's rows (files + its symbols + edges from those symbols).

    Deleting the file row cascades to its symbols; we then re-insert. Edges are keyed by src_id
    (a symbol in this file), so we clear those explicitly first.
    """
    path = file_row["path"]
    sym_ids = [s["id"] for s in symbols]
    conn.execute("DELETE FROM files WHERE path=?", (path,))          # cascades to symbols
    if sym_ids:
        conn.executemany("DELETE FROM edges WHERE src_id=?", [(sid,) for sid in sym_ids])
    conn.execute("DELETE FROM edges WHERE src_id LIKE ?", (f"{path}::%",))
    conn.execute(
        "INSERT INTO files(path, language, sha256, size, git_hash, n_symbols) VALUES(?,?,?,?,?,?)",
        (path, file_row["language"], file_row["sha256"], file_row["size"],
         file_row.get("git_hash"), len(symbols)),
    )
    if symbols:
        conn.executemany(
            "INSERT INTO symbols(id,file_path,kind,name,qualname,parent_id,package,start_line,"
            "end_line,signature,docstring,decorators,content_hash,rollup_hash) "
            "VALUES(:id,:file_path,:kind,:name,:qualname,:parent_id,:package,:start_line,"
            ":end_line,:signature,:docstring,:decorators,:content_hash,:rollup_hash)",
            symbols,
        )
    edge_rows = list(edges)
    if edge_rows:
        conn.executemany(
            "INSERT INTO edges(src_id,kind,dst_id,dst_name) VALUES(:src_id,:kind,:dst_id,:dst_name)",
            edge_rows,
        )


def delete_file(conn: sqlite3.Connection, path: str) -> None:
    conn.execute("DELETE FROM edges WHERE src_id LIKE ?", (f"{path}::%",))
    conn.execute("DELETE FROM files WHERE path=?", (path,))          # cascades to symbols


def all_file_hashes(conn: sqlite3.Connection) -> dict[str, str]:
    return {r["path"]: r["sha256"] for r in conn.execute("SELECT path, sha256 FROM files")}


# ------------------------------------------------------------------ domain nodes
def replace_domain_nodes(conn: sqlite3.Connection, kind: str, rows: list[dict[str, Any]]) -> None:
    conn.execute("DELETE FROM domain_nodes WHERE kind=?", (kind,))
    if rows:
        conn.executemany(
            "INSERT OR REPLACE INTO domain_nodes(id,kind,name,detail,file_path,line) "
            "VALUES(:id,:kind,:name,:detail,:file_path,:line)",
            rows,
        )


def replace_edges_of_kinds(conn: sqlite3.Connection, kinds: tuple[str, ...],
                           rows: list[dict[str, Any]]) -> None:
    """Wholesale-replace derived edges (e.g. publishes/consumes) the extractors own."""
    qmarks = ",".join("?" * len(kinds))
    conn.execute(f"DELETE FROM edges WHERE kind IN ({qmarks})", kinds)
    if rows:
        conn.executemany(
            "INSERT INTO edges(src_id,kind,dst_id,dst_name,resolved) "
            "VALUES(:src_id,:kind,:dst_id,:dst_name,:resolved)",
            rows,
        )


# ------------------------------------------------------------------ summaries
def get_summary(conn: sqlite3.Connection, node_id: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM summaries WHERE node_id=?", (node_id,)).fetchone()
    return dict(row) if row else None


def summary_hash(conn: sqlite3.Connection, node_id: str) -> str | None:
    row = conn.execute("SELECT hash FROM summaries WHERE node_id=?", (node_id,)).fetchone()
    return row["hash"] if row else None


def upsert_summary(conn: sqlite3.Connection, *, node_id: str, node_kind: str, hash_: str,
                   summary: dict[str, Any], model: str, tokens_in: int = 0, tokens_out: int = 0,
                   generated_at: str = "") -> None:
    conn.execute(
        "INSERT INTO summaries(node_id,node_kind,hash,summary_json,model,tokens_in,tokens_out,generated_at) "
        "VALUES(?,?,?,?,?,?,?,?) "
        "ON CONFLICT(node_id) DO UPDATE SET node_kind=excluded.node_kind, hash=excluded.hash, "
        "summary_json=excluded.summary_json, model=excluded.model, tokens_in=excluded.tokens_in, "
        "tokens_out=excluded.tokens_out, generated_at=excluded.generated_at",
        (node_id, node_kind, hash_, json.dumps(summary, ensure_ascii=False), model,
         tokens_in, tokens_out, generated_at),
    )


# ------------------------------------------------------------------ page builds (LLM page writer)
def get_page_build(conn: sqlite3.Connection, slug: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM page_builds WHERE slug=?", (slug,)).fetchone()
    return dict(row) if row else None


def upsert_page_build(conn: sqlite3.Connection, *, slug: str, hash_: str, git_head: str,
                      model: str, status: str, tokens_in: int = 0, tokens_out: int = 0,
                      validator: dict[str, Any] | None = None,
                      meta: dict[str, Any] | None = None, written_at: str = "",
                      skeleton: dict[str, Any] | None = None) -> None:
    conn.execute(
        "INSERT INTO page_builds(slug,hash,git_head,model,status,tokens_in,tokens_out,"
        "validator_json,meta_json,written_at,skeleton_json) VALUES(?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(slug) DO UPDATE SET hash=excluded.hash, git_head=excluded.git_head, "
        "model=excluded.model, status=excluded.status, tokens_in=excluded.tokens_in, "
        "tokens_out=excluded.tokens_out, validator_json=excluded.validator_json, "
        "meta_json=excluded.meta_json, written_at=excluded.written_at, "
        "skeleton_json=excluded.skeleton_json",
        (slug, hash_, git_head, model, status, tokens_in, tokens_out,
         json.dumps(validator or {}, ensure_ascii=False),
         json.dumps(meta or {}, ensure_ascii=False), written_at,
         json.dumps(skeleton or {}, ensure_ascii=False)),
    )


def prune_orphan_summaries(conn: sqlite3.Connection) -> int:
    """Drop symbol-level summaries whose symbol no longer exists. Returns count removed."""
    cur = conn.execute(
        "DELETE FROM summaries WHERE node_kind='symbol' AND node_id NOT IN (SELECT id FROM symbols)"
    )
    return cur.rowcount
