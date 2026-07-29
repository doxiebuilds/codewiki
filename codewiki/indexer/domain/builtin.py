"""
builtin.py — the built-in DomainExtractor set (deterministic; no LLM).

Seven framework-shaped extractors, generic across most Python/TS backend+frontend repos:

  * route        — FastAPI/Flask-style ``@router.<verb>("/path")`` / ``@app.<verb>(...)`` decorators.
  * db_table      — ``CREATE TABLE`` names in any ``*.sql`` file in the repo.
  * redis_channel — string args to ``.publish/.subscribe/.xadd/.xread(...)`` calls, WITH direction
                    (publish vs subscribe sites) and ``publishes``/``consumes`` edges from the
                    enclosing symbol — raw material for cross-process data-flow diagrams.
  * ws_event      — ``"type": "<name>"`` discriminators (a common websocket-message convention).
  * ffi_export    — Rust items marked #[pyfunction]/#[pyclass]/#[pymethods]: the Python-visible
                    Rust API, for repos with a PyO3 extension.
  * api_call      — '/api/…' string literals in frontend code, matched to backend `route` nodes.
  * env_flag      — os.environ.get/getenv reads: the runtime configuration surface.

Each is registered with ``indexer.domain.register`` at import time. Write and register your own
for anything repo-specific — see ``examples/plugins/``.
"""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

from codewiki.indexer.domain.base import register
from codewiki.paths import PROJECT_ROOT, iter_repo_files, rel_to_repo
from codewiki.store import db

_ROUTE_RE = re.compile(r"@(\w+)\.(get|post|put|delete|patch|websocket)\(\s*[\"']([^\"']+)[\"']")
_DEF_RE = re.compile(r"^\s*(?:async\s+)?def\s+(\w+)")
_TABLE_RE = re.compile(r"create\s+table(?:\s+if\s+not\s+exists)?\s+([\w.\"]+)", re.IGNORECASE)
_CHANNEL_RE = re.compile(
    r"\.(publish|subscribe|psubscribe|xadd|xread|xreadgroup)\(\s*[frbu]*[\"']([^\"']+)[\"']")
_WS_TYPE_RE = re.compile(r"[\"']type[\"']\s*:\s*[\"']([a-zA-Z0-9_.]+)[\"']")
_API_CALL_RE = re.compile(r"[\"'`](/api/[^\"'`\s]+)[\"'`]")
_ENV_RE = re.compile(
    r"os\.(?:environ\.get|getenv)\(\s*[\"']([A-Z][A-Z0-9_]{2,})[\"'](?:\s*,\s*([^),]{1,40}))?")
_ENV_IDX_RE = re.compile(r"os\.environ\[[\"']([A-Z][A-Z0-9_]{2,})[\"']\]")
_TEMPLATE_RE = re.compile(r"\$\{[^}]*\}|\{[^}]*\}")

# op → data-flow direction (edge kind)
_CHANNEL_EDGE_KIND = {"publish": "publishes", "xadd": "publishes",
                      "subscribe": "consumes", "psubscribe": "consumes",
                      "xread": "consumes", "xreadgroup": "consumes"}
_FFI_ATTRS = ("pyfunction", "pyclass", "pymethods")


def _iter_py(root: Path):
    yield from iter_repo_files(root, suffixes={".py"})


def _enclosing_symbol_id(conn: sqlite3.Connection, file_path: str, line: int) -> str | None:
    """Innermost function/method containing `line`, else the file's module symbol."""
    row = conn.execute(
        "SELECT id FROM symbols WHERE file_path=? AND kind IN ('function','method') "
        "AND start_line<=? AND end_line>=? ORDER BY (end_line-start_line) LIMIT 1",
        (file_path, line, line)).fetchone()
    if row:
        return row["id"]
    row = conn.execute(
        "SELECT id FROM symbols WHERE file_path=? AND kind='module' LIMIT 1", (file_path,)).fetchone()
    return row["id"] if row else None


# ------------------------------------------------------------------ extractors
def extract_routes(conn: sqlite3.Connection) -> list[dict]:
    rows, seen = [], set()
    for p in _iter_py(PROJECT_ROOT):
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
        rel = rel_to_repo(p)
        for i, line in enumerate(lines):
            m = _ROUTE_RE.search(line)
            if not m:
                continue
            _, verb, route = m.groups()
            func = ""
            for j in range(i + 1, min(i + 6, len(lines))):
                dm = _DEF_RE.match(lines[j])
                if dm:
                    func = dm.group(1)
                    break
            key = f"{verb.upper()} {route}"
            if key in seen:
                continue
            seen.add(key)
            rows.append({"id": f"route::{key}", "kind": "route", "name": route,
                         "detail": json.dumps({"method": verb.upper(), "func": func}),
                         "file_path": rel, "line": i + 1})
    return rows


def extract_db_tables(conn: sqlite3.Connection) -> list[dict]:
    rows, seen = [], set()
    for p in iter_repo_files(PROJECT_ROOT, suffixes={".sql"}):
        text = p.read_text(encoding="utf-8", errors="replace")
        rel = rel_to_repo(p)
        for m in _TABLE_RE.finditer(text):
            name = m.group(1).strip('"').split(".")[-1]
            if name in seen:
                continue
            seen.add(name)
            line = text[:m.start()].count("\n") + 1
            rows.append({"id": f"table::{name}", "kind": "db_table", "name": name,
                         "detail": json.dumps({}), "file_path": rel, "line": line})
    return rows


def extract_redis_channels(conn: sqlite3.Connection) -> list[dict]:
    """One node per channel with per-direction site counts; plus publishes/consumes edges."""
    # channel -> {"publish": [(path, line)], "subscribe": [(path, line)]}
    sites: dict[str, dict[str, list[tuple[str, int]]]] = {}
    for p in _iter_py(PROJECT_ROOT):
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if not any(op in text for op in (".publish(", ".subscribe(", ".psubscribe(",
                                         ".xadd(", ".xread")):
            continue
        rel = rel_to_repo(p)
        for m in _CHANNEL_RE.finditer(text):
            op, chan = m.groups()
            # keep channel-shaped literals (namespaced or templated), drop noise
            if not re.search(r"[:_]", chan) or " " in chan:
                continue
            line = text[:m.start()].count("\n") + 1
            direction = "publish" if _CHANNEL_EDGE_KIND[op] == "publishes" else "subscribe"
            sites.setdefault(chan, {"publish": [], "subscribe": []})[direction].append((rel, line))

    rows, edge_rows = [], []
    for chan, by_dir in sorted(sites.items()):
        first = (by_dir["publish"] + by_dir["subscribe"])[0]
        detail = {"n_publish": len(by_dir["publish"]), "n_subscribe": len(by_dir["subscribe"])}
        for direction in ("publish", "subscribe"):
            if by_dir[direction]:
                detail[direction] = f"{by_dir[direction][0][0]}:{by_dir[direction][0][1]}"
        rows.append({"id": f"chan::{chan}", "kind": "redis_channel", "name": chan,
                     "detail": json.dumps(detail), "file_path": first[0], "line": first[1]})
        for direction, edge_kind in (("publish", "publishes"), ("subscribe", "consumes")):
            for rel, line in by_dir[direction]:
                src = _enclosing_symbol_id(conn, rel, line)
                if src:
                    edge_rows.append({"src_id": src, "kind": edge_kind, "dst_id": None,
                                      "dst_name": chan, "resolved": ""})
    db.replace_edges_of_kinds(conn, ("publishes", "consumes"), edge_rows)
    return rows


def extract_ws_events(conn: sqlite3.Connection) -> list[dict]:
    rows, seen = [], set()
    for p in _iter_py(PROJECT_ROOT):
        text = p.read_text(encoding="utf-8", errors="replace")
        if '"type"' not in text and "'type'" not in text:
            continue
        rel = rel_to_repo(p)
        for m in _WS_TYPE_RE.finditer(text):
            ev = m.group(1)
            if ev in seen or len(ev) < 3:
                continue
            seen.add(ev)
            line = text[:m.start()].count("\n") + 1
            rows.append({"id": f"ws::{ev}", "kind": "ws_event", "name": ev,
                         "detail": json.dumps({}), "file_path": rel, "line": line})
    return rows


def extract_ffi_exports(conn: sqlite3.Connection) -> list[dict]:
    """Rust items attributed #[pyfunction]/#[pyclass]/#[pymethods] = the Python-visible API."""
    rows = []
    marked = conn.execute(
        "SELECT s.id, s.kind, s.name, s.qualname, s.signature, s.file_path, s.start_line, "
        "s.decorators FROM symbols s JOIN files f ON f.path=s.file_path "
        "WHERE f.language='rust' AND s.decorators != '[]'").fetchall()
    for sym in marked:
        attrs = " ".join(json.loads(sym["decorators"] or "[]")).lower()
        if not any(a in attrs for a in _FFI_ATTRS):
            continue
        if "pymethods" in attrs and sym["kind"] == "class":
            # every method of the #[pymethods] impl block is exported. Methods may be parented
            # to the same-named struct symbol (struct + impl share a qualname), so match by
            # qualname prefix within the impl's line span rather than parent_id.
            for m in conn.execute(
                    "SELECT name, qualname, signature, file_path, start_line FROM symbols "
                    "WHERE file_path=? AND kind='method' AND qualname LIKE ? "
                    "AND start_line>=? ORDER BY start_line",
                    (sym["file_path"], f"{sym['qualname']}::%", sym["start_line"])):
                rows.append({"id": f"ffi::{m['qualname']}", "kind": "ffi_export",
                             "name": m["qualname"],
                             "detail": json.dumps({"kind": "method",
                                                   "signature": m["signature"]}),
                             "file_path": m["file_path"], "line": m["start_line"]})
        else:
            label = sym["qualname"] or sym["name"]
            rows.append({"id": f"ffi::{label}", "kind": "ffi_export", "name": label,
                         "detail": json.dumps({"kind": sym["kind"],
                                               "signature": sym["signature"]}),
                         "file_path": sym["file_path"], "line": sym["start_line"]})
    # dedup (a struct can carry both #[pyclass] and appear via pymethods children)
    uniq: dict[str, dict] = {}
    for r in rows:
        uniq.setdefault(r["id"], r)
    return list(uniq.values())


def _route_matches(frontend_path: str, route_name: str) -> bool:
    """Segment-wise match, `{param}`/`${…}` segments wildcard; tolerate the /api prefix."""
    f = _TEMPLATE_RE.sub("*", frontend_path.split("?")[0]).strip("/").split("/")
    r = _TEMPLATE_RE.sub("*", route_name).strip("/").split("/")
    if f and f[0] == "api" and (not r or r[0] != "api"):
        f = f[1:]
    if len(f) != len(r):
        return False
    return all(a == b or a == "*" or b == "*" for a, b in zip(f, r))


def extract_api_calls(conn: sqlite3.Connection) -> list[dict]:
    """'/api/…' literals in frontend code, matched against backend route nodes."""
    routes = conn.execute(
        "SELECT name, detail FROM domain_nodes WHERE kind='route'").fetchall()
    sites: dict[str, dict] = {}
    for p in iter_repo_files(PROJECT_ROOT, suffixes={".ts", ".tsx", ".js", ".jsx"}):
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if "/api/" not in text:
            continue
        rel = rel_to_repo(p)
        for m in _API_CALL_RE.finditer(text):
            norm = _TEMPLATE_RE.sub("{param}", m.group(1).split("?")[0])
            line = text[:m.start()].count("\n") + 1
            entry = sites.setdefault(norm, {"first": (rel, line), "n": 0})
            entry["n"] += 1

    rows = []
    for norm, entry in sorted(sites.items()):
        matched = ""
        hits = [r for r in routes if _route_matches(norm, r["name"])]
        if len(hits) == 1:
            matched = f"{json.loads(hits[0]['detail']).get('method', '')} {hits[0]['name']}".strip()
        rows.append({"id": f"apicall::{norm}", "kind": "api_call", "name": norm,
                     "detail": json.dumps({"route": matched, "n_sites": entry["n"]}),
                     "file_path": entry["first"][0], "line": entry["first"][1]})
    return rows


def extract_env_flags(conn: sqlite3.Connection) -> list[dict]:
    """os.environ.get / os.getenv reads — the runtime configuration surface, per area."""
    flags: dict[str, dict] = {}
    for p in _iter_py(PROJECT_ROOT):
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if "os.environ" not in text and "os.getenv" not in text:
            continue
        rel = rel_to_repo(p)
        for m in _ENV_RE.finditer(text):
            name, default = m.group(1), (m.group(2) or "").strip()
            line = text[:m.start()].count("\n") + 1
            entry = flags.setdefault(name, {"first": (rel, line), "n": 0, "default": default})
            entry["n"] += 1
        for m in _ENV_IDX_RE.finditer(text):
            line = text[:m.start()].count("\n") + 1
            entry = flags.setdefault(m.group(1), {"first": (rel, line), "n": 0, "default": ""})
            entry["n"] += 1
    rows = []
    for name, entry in sorted(flags.items()):
        rows.append({"id": f"env::{name}", "kind": "env_flag", "name": name,
                     "detail": json.dumps({"default": entry["default"], "n_sites": entry["n"]}),
                     "file_path": entry["first"][0], "line": entry["first"][1]})
    return rows


# ------------------------------------------------------------------ registration
# NOTE: order matters — api_call reads the route nodes written earlier in the same pass.
register("route", extract_routes)
register("db_table", extract_db_tables)
register("redis_channel", extract_redis_channels)
register("ws_event", extract_ws_events)
register("ffi_export", extract_ffi_exports)
register("api_call", extract_api_calls, after="route")
register("env_flag", extract_env_flags)
