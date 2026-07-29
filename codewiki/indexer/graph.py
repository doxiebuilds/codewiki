"""
graph.py — turn parses into code-graph rows (symbols, edges, rollup hashes) and persist them.

Deterministic, no LLM. The key computation here is the **rollup hash**: leaves hash their source
span; containers hash their signature + sorted child rollup hashes. So editing one method changes
that method's hash, its class's rollup, and its module's rollup — and nothing else. That cascade
is exactly the set the summarizer must regenerate, and it's all known before any LLM runs.

Also exposes ``build_digraph`` (networkx) for the assembler's dependency/call diagrams.
"""

from __future__ import annotations

import hashlib
import sqlite3
import json
from pathlib import PurePosixPath

import networkx as nx

from codewiki.indexer.discovery import FileMeta
from codewiki.indexer.parsers import Symbol, parse_source
from codewiki.paths import PARSED_LANGS
from codewiki.store import db


def package_of(path: str) -> str:
    """Owning directory (repo-relative) used as the package grouping key."""
    return str(PurePosixPath(path).parent)


def symbol_id(path: str, qualname: str, kind: str) -> str:
    return f"{path}::{qualname or '<module>'}::{kind}"


def _rollup(symbols: list[Symbol]) -> dict[int, str]:
    """Map id(symbol) -> rollup_hash. Module is root; classes contain methods; leaves == span hash."""
    by_parent: dict[str | None, list[Symbol]] = {}
    module = None
    for s in symbols:
        if s.kind == "module":
            module = s
            continue
        by_parent.setdefault(s.parent_qualname, []).append(s)

    memo: dict[int, str] = {}

    def compute(sym: Symbol) -> str:
        if id(sym) in memo:
            return memo[id(sym)]
        children = by_parent.get(sym.qualname, []) if sym.kind in {"class", "module"} else []
        if not children:
            h = sym.content_hash
        else:
            joined = sym.signature + "|" + "".join(sorted(compute(c) for c in children))
            h = hashlib.sha256(joined.encode("utf-8")).hexdigest()
        memo[id(sym)] = h
        return h

    for s in symbols:
        if s.kind != "module":
            compute(s)
    if module is not None:
        # module children are the top-level (parent_qualname None) symbols
        top = by_parent.get(None, [])
        joined = f"module:{module.name}|" + "".join(sorted(compute(c) for c in top))
        memo[id(module)] = hashlib.sha256(joined.encode("utf-8")).hexdigest() if top else module.content_hash
    return memo


def _rows_for_file(fm: FileMeta) -> tuple[dict, list[dict], list[dict]]:
    """Return (file_row, symbol_rows, edge_rows) for one file."""
    pkg = package_of(fm.path)
    file_row = {"path": fm.path, "language": fm.language, "sha256": fm.sha256, "size": fm.size,
                "git_hash": None}

    def _file_only_row() -> dict:
        # file-level-only: a single module symbol so config/sql can still be cited & summarized.
        # end_line = real line count so `path:N` citations inside the file validate.
        try:
            n_lines = fm.abs.read_bytes().count(b"\n") + 1
        except OSError:
            n_lines = 1
        return {"id": symbol_id(fm.path, "", "module"), "file_path": fm.path, "kind": "module",
                "name": PurePosixPath(fm.path).name, "qualname": "", "parent_id": None,
                "package": pkg, "start_line": 1, "end_line": n_lines, "signature": "",
                "docstring": "", "decorators": "[]", "content_hash": fm.sha256,
                "rollup_hash": fm.sha256}

    if fm.language not in PARSED_LANGS:
        return file_row, [_file_only_row()], []

    try:
        fp = parse_source(fm.path, fm.language, fm.abs.read_bytes())
    except Exception:
        fp = None
    if fp is None or not fp.symbols:
        return file_row, [_file_only_row()], []

    rollups = _rollup(fp.symbols)
    # qualname+kind -> id (for parent + call resolution within file)
    id_by_qual: dict[str, str] = {}
    used: set[str] = set()
    sym_rows: list[dict] = []
    for s in fp.symbols:
        sid = symbol_id(fm.path, s.qualname, s.kind)
        while sid in used:
            sid += "~"
        used.add(sid)
        id_by_qual[f"{s.qualname}::{s.kind}"] = sid
        id_by_qual.setdefault(s.qualname, sid)
        sym_rows.append({
            "id": sid, "file_path": fm.path, "kind": s.kind, "name": s.name,
            "qualname": s.qualname, "parent_id": None, "package": pkg,
            "start_line": s.start_line, "end_line": s.end_line, "signature": s.signature,
            "docstring": s.docstring, "decorators": json.dumps(s.decorators),
            "content_hash": s.content_hash, "rollup_hash": rollups.get(id(s), s.content_hash),
            "_sym": s,
        })

    # names of local functions/methods (last segment) for call resolution
    local_targets: dict[str, str] = {}
    for row in sym_rows:
        if row["kind"] in {"function", "method"}:
            local_targets.setdefault(row["_sym"].name, row["id"])

    edge_rows: list[dict] = []
    module_id = next((r["id"] for r in sym_rows if r["kind"] == "module"), None)
    for row in sym_rows:
        s: Symbol = row["_sym"]
        parent_id = (id_by_qual.get(s.parent_qualname) if s.parent_qualname else module_id) \
            if s.kind != "module" else None
        row["parent_id"] = parent_id
        if parent_id and s.kind != "module":
            edge_rows.append({"src_id": parent_id, "kind": "contains", "dst_id": row["id"],
                              "dst_name": s.qualname})
        for callee in s.calls:
            edge_rows.append({"src_id": row["id"], "kind": "calls",
                              "dst_id": local_targets.get(callee), "dst_name": callee})
    if module_id:
        for imp in fp.imports:
            edge_rows.append({"src_id": module_id, "kind": "imports", "dst_id": None,
                              "dst_name": imp.module or imp.raw})

    for row in sym_rows:
        row.pop("_sym", None)
    return file_row, sym_rows, edge_rows


def index_file(conn: sqlite3.Connection, fm: FileMeta) -> int:
    file_row, sym_rows, edge_rows = _rows_for_file(fm)
    db.replace_file(conn, file_row, sym_rows, edge_rows)
    return len(sym_rows)


def build_digraph(conn: sqlite3.Connection) -> nx.DiGraph:
    """Symbol-level DiGraph (contains/calls/imports) for diagram + reachability queries."""
    g = nx.DiGraph()
    for r in conn.execute("SELECT id, kind, name, package, file_path FROM symbols"):
        g.add_node(r["id"], kind=r["kind"], name=r["name"], package=r["package"],
                   file=r["file_path"])
    for e in conn.execute("SELECT src_id, kind, dst_id, dst_name FROM edges"):
        if e["dst_id"]:
            g.add_edge(e["src_id"], e["dst_id"], kind=e["kind"], name=e["dst_name"])
    return g
