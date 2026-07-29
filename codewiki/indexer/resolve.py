"""
resolve.py — global, import-aware resolution of dangling call/import edges (deterministic; no LLM).

graph.py sees one file at a time, so it can only resolve calls *within* that file; every
cross-file call and every import is stored dangling (``dst_id`` NULL, ``dst_name`` = raw text).
The architecturally interesting flows (a caller in one package reaching a callee in another)
are exactly the dangling ones. This pass runs once per index, repo-wide:

  imports:  Python dotted / relative, TS ``./``/``../`` and Rust ``use crate::…`` strings are
            mapped to the target file's module symbol.
  calls:    (a) callee defined in a file this module imports → that symbol   resolved='import'
            (b) callee name unique repo-wide (functions/methods/classes)     resolved='unique'
            graph.py's same-file matches are tagged                          resolved='local'

Confidence lands in ``edges.resolved`` so consumers (summary contexts, page bundles, diagrams)
can decide how much to trust an arrow. Ambiguous names stay dangling — never guess.
"""

from __future__ import annotations

import posixpath
import sqlite3
from dataclasses import dataclass
from pathlib import PurePosixPath

from codewiki import config as C

_TS_EXTS = (".ts", ".tsx", ".js", ".jsx")
_KIND_PREFERENCE = {"function": 0, "class": 1, "method": 2}


@dataclass
class ResolveStats:
    imports_resolved: int = 0
    imports_total: int = 0
    calls_import: int = 0
    calls_unique: int = 0
    calls_local: int = 0
    calls_unresolved: int = 0


def module_symbol_id(path: str) -> str:
    return f"{path}::<module>::module"


# ------------------------------------------------------------------ per-language candidates
def _python_candidates(dst: str, src_file: str) -> list[str]:
    """Candidate repo-relative paths for a python import string (dotted or relative)."""
    dst = dst.strip()
    if dst.startswith("import "):                       # plain `import a.b [as c]`
        dst = dst[len("import "):].split(",")[0].split(" as ")[0].strip()
    if not dst:
        return []
    if dst.startswith("."):                             # `from ..a.b import c`
        dots = len(dst) - len(dst.lstrip("."))
        rest = dst.lstrip(".")
        base = PurePosixPath(src_file).parent
        for _ in range(dots - 1):
            base = base.parent
        prefix, parts = str(base), (rest.split(".") if rest else [])
    else:                                               # absolute, rooted at the project dir
        prefix, parts = C.load().source_subdir, dst.split(".")
    out: list[str] = []
    for cut in range(len(parts), 0, -1):                # shorter prefixes: `import a.b.NAME`
        stem = "/".join([p for p in (prefix, *parts[:cut]) if p])
        out += [stem + ".py", stem + "/__init__.py"]
    if not parts:
        out.append(f"{prefix}/__init__.py" if prefix else "__init__.py")
    return out


def _ts_candidates(dst: str, src_file: str) -> list[str]:
    dst = dst.strip()
    if not dst.startswith("."):
        return []                                       # bare specifier = npm package (external)
    stem = posixpath.normpath(posixpath.join(str(PurePosixPath(src_file).parent), dst))
    if stem.endswith(_TS_EXTS):
        return [stem]
    return [stem + ext for ext in _TS_EXTS] + [stem + "/index" + ext for ext in _TS_EXTS]


def _rust_candidates(dst_raw: str, src_file: str) -> list[str]:
    """Map `use crate::a::b::Item;` (or super/self) to a/b.rs | a/b/mod.rs within the crate."""
    s = dst_raw.strip()
    if s.startswith("use "):
        s = s[4:]
    s = s.rstrip(";").strip().split("{", 1)[0].rstrip(": ").split(" as ")[0].strip()
    parts = [p for p in s.split("::") if p]
    if not parts:
        return []
    src_dir = str(PurePosixPath(src_file).parent)
    if parts[0] == "crate":
        idx = src_file.find("/src/")
        root = src_file[: idx + 4] if idx != -1 else src_dir
    elif parts[0] == "super":
        root = str(PurePosixPath(src_dir).parent)
    elif parts[0] == "self":
        root = src_dir
    else:
        return []                                       # external crate
    parts = parts[1:]
    out: list[str] = []
    for cut in range(len(parts), 0, -1):                # last segment(s) are items, not modules
        stem = "/".join([root, *parts[:cut]])
        out += [stem + ".rs", stem + "/mod.rs"]
    return out


_CANDIDATES = {"python": _python_candidates, "typescript": _ts_candidates,
               "javascript": _ts_candidates, "rust": _rust_candidates}


# ------------------------------------------------------------------ the pass
def resolve_all(conn: sqlite3.Connection) -> ResolveStats:
    stats = ResolveStats()
    files = {r["path"]: r["language"] for r in conn.execute("SELECT path, language FROM files")}

    # drop resolutions pointing at symbols that no longer exist (deleted/renamed files)
    conn.execute(
        "UPDATE edges SET dst_id=NULL, resolved='' WHERE kind IN ('calls','imports') "
        "AND dst_id IS NOT NULL AND dst_id NOT IN (SELECT id FROM symbols)")
    # tag graph.py's same-file call resolutions
    conn.execute(
        "UPDATE edges SET resolved='local' WHERE kind='calls' AND dst_id IS NOT NULL AND resolved=''")

    # ---- pass 1: imports → target module symbols; remember file→imported-files for pass 2
    imports_by_file: dict[str, set[str]] = {}
    upd: list[tuple[str, str, int]] = []
    for r in conn.execute(
            "SELECT e.rowid rid, e.dst_name, s.file_path FROM edges e "
            "JOIN symbols s ON s.id=e.src_id WHERE e.kind='imports'"):
        stats.imports_total += 1
        fn = _CANDIDATES.get(files.get(r["file_path"], ""))
        target = None
        if fn is not None:
            target = next((c for c in fn(r["dst_name"], r["file_path"]) if c in files), None)
        if target and target != r["file_path"]:
            imports_by_file.setdefault(r["file_path"], set()).add(target)
            upd.append((module_symbol_id(target), "import", r["rid"]))
            stats.imports_resolved += 1
    conn.executemany("UPDATE edges SET dst_id=?, resolved=? WHERE rowid=?", upd)

    # ---- pass 2: dangling calls → imported-file match, else unique-name match
    name_index: dict[str, list[tuple[str, str, str]]] = {}   # name -> [(id, file, kind)]
    for r in conn.execute(
            "SELECT id, name, file_path, kind FROM symbols "
            "WHERE kind IN ('function','method','class')"):
        name_index.setdefault(r["name"], []).append((r["id"], r["file_path"], r["kind"]))

    upd = []
    for r in conn.execute(
            "SELECT e.rowid rid, e.dst_name, s.file_path FROM edges e "
            "JOIN symbols s ON s.id=e.src_id WHERE e.kind='calls' AND e.dst_id IS NULL"):
        cands = name_index.get(r["dst_name"].rstrip("!"), [])   # rust macros carry trailing '!'
        if not cands:
            stats.calls_unresolved += 1
            continue
        imported = imports_by_file.get(r["file_path"], set())
        hits = sorted((c for c in cands if c[1] in imported),
                      key=lambda c: _KIND_PREFERENCE.get(c[2], 9))
        if hits and (len(hits) == 1 or hits[0][2] != hits[1][2]):   # unambiguous best kind
            upd.append((hits[0][0], "import", r["rid"]))
            stats.calls_import += 1
        elif len(cands) == 1:
            upd.append((cands[0][0], "unique", r["rid"]))
            stats.calls_unique += 1
        else:
            stats.calls_unresolved += 1
    conn.executemany("UPDATE edges SET dst_id=?, resolved=? WHERE rowid=?", upd)

    stats.calls_local = conn.execute(
        "SELECT COUNT(*) n FROM edges WHERE kind='calls' AND resolved='local'").fetchone()["n"]
    return stats
