"""
run.py — drive a full or incremental index into the code-graph DB (deterministic; no LLM).

Compares each discovered file's sha256 to what's stored; only added/modified files are re-parsed
(removed files are deleted, cascading their symbols). Domain nodes are re-extracted whenever the
file set changed. This is the cheap, LLM-free stage — safe to run on every push.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone

from codewiki.indexer import discovery, domain, graph, resolve
from codewiki.store import db

SCHEMA_VERSION = "2"


@dataclass
class IndexStats:
    added: list[str] = field(default_factory=list)
    modified: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    files_total: int = 0
    symbols_total: int = 0
    domain: dict[str, int] = field(default_factory=dict)
    resolution: resolve.ResolveStats | None = None

    @property
    def changed(self) -> int:
        return len(self.added) + len(self.modified) + len(self.removed)


def run_index(conn: sqlite3.Connection, *, only_changed: bool = True) -> IndexStats:
    files = discovery.discover()
    cur = {fm.path: fm for fm in files}
    prev = db.all_file_hashes(conn)
    force_all = (not only_changed) or (not prev)

    added = sorted(p for p in cur if p not in prev)
    modified = sorted(p for p in cur if p in prev and cur[p].sha256 != prev[p])
    removed = sorted(p for p in prev if p not in cur)

    to_index = list(cur.values()) if force_all else [cur[p] for p in (added + modified)]

    for path in removed:
        db.delete_file(conn, path)
    for fm in to_index:
        graph.index_file(conn, fm)

    stats = IndexStats(added=added, modified=modified, removed=removed, files_total=len(cur))

    if force_all or stats.changed:
        stats.domain = domain.index_domain(conn)
        stats.resolution = resolve.resolve_all(conn)

    row = conn.execute("SELECT COUNT(*) AS n FROM symbols").fetchone()
    stats.symbols_total = row["n"]

    db.set_meta(conn, "schema_version", SCHEMA_VERSION)
    db.set_meta(conn, "head", discovery.head_sha())
    db.set_meta(conn, "indexed_at", datetime.now(timezone.utc).isoformat())
    db.prune_orphan_summaries(conn)
    conn.commit()
    return stats
