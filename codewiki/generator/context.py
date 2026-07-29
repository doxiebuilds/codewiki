"""
context.py — assemble the *structured* context a node's summary needs (deterministic; no LLM).

This is the token-savings core. Instead of letting the model navigate and read whole files, we
hand it exactly what a good summary requires and nothing more:

  * leaf (function/method): its own (bounded) source span + signature + docstring + who calls it,
    what it calls, its module's imports, and sibling names.
  * container (class/module): the *summaries* of its children — never their source again.
  * package: the summaries of its modules.
  * page: the summaries of its packages/modules + the relevant domain-node reference rows.

A one-function edit therefore re-reads one function body, then rolls up through cheap
summary-of-summaries prompts.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import PurePosixPath

from codewiki.paths import REPO_ROOT

MAX_SPAN_CHARS = 9000


def _read_span(file_path: str, start: int, end: int, max_chars: int = MAX_SPAN_CHARS) -> str:
    """Source span, elided at line boundaries when oversized.

    A hard character cut loses the return/emission paths of large functions — the part the
    summarizer needs most. Instead keep the head (~65%) and tail (~35%) with an explicit
    elision marker, so both the signature/setup AND the exit paths survive.
    """
    try:
        lines = (REPO_ROOT / file_path).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    span = lines[max(0, start - 1):end]
    snippet = "\n".join(span)
    if len(snippet) <= max_chars:
        return snippet
    head_budget, tail_budget = int(max_chars * 0.65), int(max_chars * 0.35)
    head: list[str] = []
    used = 0
    for line in span:
        if used + len(line) + 1 > head_budget:
            break
        head.append(line)
        used += len(line) + 1
    tail: list[str] = []
    used = 0
    for line in reversed(span[len(head):]):
        if used + len(line) + 1 > tail_budget:
            break
        tail.insert(0, line)
        used += len(line) + 1
    elided = len(span) - len(head) - len(tail)
    return "\n".join(head + [f"# … [{elided} lines elided] …"] + tail)


def _callees(conn: sqlite3.Connection, sym_id: str) -> list[str]:
    """Callee names, enriched with the resolved target when resolve.py found one.

    `process → handlers.py:OrderService.process` is actionable context; a bare token is not.
    """
    rows = conn.execute(
        "SELECT DISTINCT e.dst_name, d.qualname dq, d.file_path df "
        "FROM edges e LEFT JOIN symbols d ON d.id=e.dst_id "
        "WHERE e.src_id=? AND e.kind='calls' LIMIT 30", (sym_id,))
    out: list[str] = []
    seen: set[str] = set()
    for r in rows:
        if r["df"]:
            label = f"{r['dst_name']} → {PurePosixPath(r['df']).name}:{r['dq'] or '<module>'}"
        else:
            label = r["dst_name"]
        if label not in seen:
            seen.add(label)
            out.append(label)
    return out


def _callers(conn: sqlite3.Connection, sym_id: str) -> list[str]:
    rows = conn.execute(
        "SELECT DISTINCT s.qualname, s.file_path FROM edges e JOIN symbols s ON s.id=e.src_id "
        "WHERE e.dst_id=? AND e.kind='calls' LIMIT 20", (sym_id,))
    out = []
    for r in rows:
        mod = PurePosixPath(r["file_path"]).name
        out.append(f"{mod}:{r['qualname']}" if r["qualname"] else mod)
    return out


def _module_imports(conn: sqlite3.Connection, file_path: str) -> list[str]:
    row = conn.execute(
        "SELECT id FROM symbols WHERE file_path=? AND kind='module' LIMIT 1", (file_path,)).fetchone()
    if not row:
        return []
    rows = conn.execute(
        "SELECT dst_name FROM edges WHERE src_id=? AND kind='imports' LIMIT 40", (row["id"],))
    return [r["dst_name"] for r in rows]


def _siblings(conn: sqlite3.Connection, sym: sqlite3.Row) -> list[str]:
    rows = conn.execute(
        "SELECT name FROM symbols WHERE file_path=? AND kind IN ('function','method','class') "
        "AND id!=? LIMIT 40", (sym["file_path"], sym["id"]))
    return [r["name"] for r in rows]


def leaf_context(conn: sqlite3.Connection, sym: sqlite3.Row) -> str:
    """Human-readable context block for a function/method/leaf-class."""
    parts = [
        f"LANGUAGE: {conn.execute('SELECT language FROM files WHERE path=?', (sym['file_path'],)).fetchone()['language']}",
        f"FILE: {sym['file_path']}",
        f"SYMBOL: {sym['kind']} {sym['qualname'] or sym['name']}  (lines {sym['start_line']}-{sym['end_line']})",
        f"SIGNATURE: {sym['signature']}" if sym["signature"] else "",
    ]
    decorators = json.loads(sym["decorators"] or "[]")
    if decorators:
        parts.append("DECORATORS: " + ", ".join(decorators))
    if sym["docstring"]:
        parts.append(f"DOCSTRING: {sym['docstring']}")
    callees = _callees(conn, sym["id"])
    if callees:
        parts.append("CALLS: " + ", ".join(callees))
    callers = _callers(conn, sym["id"])
    if callers:
        parts.append("CALLED BY: " + ", ".join(callers))
    imports = _module_imports(conn, sym["file_path"])
    if imports:
        parts.append("MODULE IMPORTS: " + ", ".join(imports[:20]))
    siblings = _siblings(conn, sym)
    if siblings:
        parts.append("SIBLINGS IN FILE: " + ", ".join(siblings[:25]))
    src = _read_span(sym["file_path"], sym["start_line"], sym["end_line"])
    if src:
        parts.append("SOURCE:\n" + src)
    return "\n".join(p for p in parts if p)


def _child_summaries(conn: sqlite3.Connection, parent_id: str) -> list[tuple[str, str]]:
    """(label, one-line summary) for each direct child that has a stored summary."""
    rows = conn.execute(
        "SELECT s.name, s.kind, s.qualname, su.summary_json FROM symbols s "
        "JOIN summaries su ON su.node_id=s.id WHERE s.parent_id=? ORDER BY s.start_line", (parent_id,))
    out = []
    for r in rows:
        try:
            summ = json.loads(r["summary_json"]).get("summary", "")
        except json.JSONDecodeError:
            summ = ""
        out.append((f"{r['kind']} {r['qualname'] or r['name']}", summ))
    return out


def container_context(conn: sqlite3.Connection, sym: sqlite3.Row) -> str:
    """Context for a class/module: the child summaries (never re-reading source)."""
    parts = [
        f"FILE: {sym['file_path']}",
        f"{sym['kind'].upper()}: {sym['qualname'] or sym['name']}",
        f"SIGNATURE: {sym['signature']}" if sym["signature"] else "",
    ]
    if sym["docstring"]:
        parts.append(f"DOCSTRING: {sym['docstring']}")
    children = _child_summaries(conn, sym["id"])
    if children:
        parts.append("MEMBERS:")
        parts.extend(f"  - {label}: {summ}" for label, summ in children)
        # small classes: include the source too — member summaries alone lose the actual
        # mechanism (calibration: a fixed-delay limiter got labeled "token bucket")
        if sym["kind"] == "class" and (sym["end_line"] - sym["start_line"]) <= 60:
            src = _read_span(sym["file_path"], sym["start_line"], sym["end_line"])
            if src:
                parts.append("SOURCE:\n" + src)
    else:
        src = _read_span(sym["file_path"], sym["start_line"], sym["end_line"])
        if src:
            parts.append("SOURCE:\n" + src)
    return "\n".join(p for p in parts if p)


def package_module_summaries(conn: sqlite3.Connection, package: str) -> list[tuple[str, str]]:
    rows = conn.execute(
        "SELECT s.file_path, su.summary_json FROM symbols s "
        "JOIN summaries su ON su.node_id=s.id WHERE s.kind='module' AND s.package=? "
        "ORDER BY s.file_path", (package,))
    out = []
    for r in rows:
        try:
            summ = json.loads(r["summary_json"]).get("summary", "")
        except json.JSONDecodeError:
            summ = ""
        out.append((PurePosixPath(r["file_path"]).name, summ))
    return out
