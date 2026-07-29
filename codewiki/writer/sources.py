"""
sources.py — per-section Sources blocks: parse, repair, and render as GitHub blob links.

Sections never put file paths in prose; each one ends with a bare-token block

    **Sources:**
    - apps/foo/bar.py:12-80

whose entries must fall inside that section's evidence locations (±5-line slack). This module
repairs what is mechanically fixable (rootless paths, over-long ranges), drops what is not
(unknown files, out-of-slice ranges), rewrites stray inline paths in prose down to backticked
basenames, and — as the LAST page pass — converts every surviving bare token into a
``[basename:a-b](https://github.com/...#La-Lb)`` link (or, when no GitHub base is configured,
a plain ``path:a-b`` bullet — see ``config.github_blob_base``).
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import PurePosixPath

from codewiki import config as C

SLACK = 5                                   # ± lines an entry may drift from its evidence range

SOURCES_HEAD_RE = re.compile(r"^\*\*Sources:\*\*\s*$")
ENTRY_RE = re.compile(r"^\s*[-*]\s+`?([\w][\w./\-]*?):(\d+)(?:-(\d+))?`?\s*$")
LOC_RE = re.compile(r"^([\w][\w./\-]*):(\d+)(?:-(\d+))?$")
# repo-ish path in prose (with at least one dir segment and a code extension)
INLINE_PATH_RE = re.compile(
    r"`?(?:[\w.\-]+/)+[\w.\-]+\."
    r"(?:py|rs|ts|tsx|js|jsx|sql|ya?ml|toml|json|sh)(?::\d+(?:-\d+)?)?`?")


def parse_loc(token: str) -> tuple[str, int, int | None] | None:
    m = LOC_RE.match(token.strip())
    if not m:
        return None
    return m.group(1), int(m.group(2)), int(m.group(3)) if m.group(3) else None


def _file_max_line(conn: sqlite3.Connection, path: str) -> int | None:
    row = conn.execute("SELECT MAX(end_line) m FROM symbols WHERE file_path=?", (path,)).fetchone()
    return row["m"] if row and row["m"] else None


# ------------------------------------------------------------------ parse
def split_sources(section_md: str) -> tuple[str, list[str] | None]:
    """(body, entries) — entries is None when the section has NO Sources block at all."""
    lines = section_md.splitlines()
    head_idx = None
    for i, line in enumerate(lines):
        if SOURCES_HEAD_RE.match(line.strip()):
            head_idx = i                            # last block wins
    if head_idx is None:
        return section_md, None
    entries: list[str] = []
    tail_rest: list[str] = []
    for line in lines[head_idx + 1:]:
        m = ENTRY_RE.match(line)
        if m:
            rng = f"{m.group(1)}:{m.group(2)}" + (f"-{m.group(3)}" if m.group(3) else "")
            entries.append(rng)
        elif line.strip():
            tail_rest.append(line)
    body = "\n".join(lines[:head_idx] + tail_rest).rstrip()
    return body, entries


# ------------------------------------------------------------------ repair
def repair_entries(conn: sqlite3.Connection, entries: list[str],
                   allowed: set[str] | None) -> tuple[list[tuple[str, int, int | None]], list[str]]:
    """Repair/drop Sources entries. Returns (kept, notes).

    Same semantics as validate.repair_citations — rootless paths get the repo-root prefix when
    that file exists, range ends clamp to the file's max line, unknown paths drop — PLUS entries
    outside ``allowed`` (the section's evidence locations, ±SLACK lines) drop.
    """
    known = {r["path"] for r in conn.execute("SELECT path FROM files")}
    allowed_ranges: list[tuple[str, int, int]] = []
    if allowed is not None:
        for tok in allowed:
            loc = parse_loc(tok)
            if loc:
                allowed_ranges.append((loc[0], loc[1], loc[2] if loc[2] else loc[1]))

    kept: list[tuple[str, int, int | None]] = []
    notes: list[str] = []
    seen: set[tuple] = set()
    for raw in entries:
        loc = parse_loc(raw)
        if loc is None:
            notes.append(f"unparseable Sources entry dropped: {raw!r}")
            continue
        path, a, bnd = loc
        if path not in known:
            prefix = C.root_prefix()
            cand = f"{prefix}{path}" if prefix else None
            if cand and cand in known:
                path = cand
                notes.append(f"prefixed rootless path: {raw}")
            else:
                notes.append(f"unknown file dropped from Sources: {raw}")
                continue
        max_line = _file_max_line(conn, path)
        if max_line is not None:
            if a > max_line:
                notes.append(f"start line past end of file — dropped: {raw}")
                continue
            if bnd is not None and bnd > max_line:
                bnd = max_line
                notes.append(f"clamped range end to {max_line}: {raw}")
        if allowed is not None:
            end = bnd if bnd is not None else a
            ok = any(p == path and a >= lo - SLACK and end <= hi + SLACK
                     for p, lo, hi in allowed_ranges)
            if not ok:
                notes.append(f"outside this section's evidence — dropped: {raw}")
                continue
        key = (path, a, bnd)
        if key in seen:
            continue
        seen.add(key)
        kept.append(key)
    return kept, notes


# ------------------------------------------------------------------ render
def render_bare(entries: list[tuple[str, int, int | None]]) -> str:
    lines = ["**Sources:**"]
    for path, a, bnd in entries:
        lines.append(f"- {path}:{a}-{bnd}" if bnd is not None else f"- {path}:{a}")
    return "\n".join(lines)


def render_sources(entries: list[tuple[str, int, int | None]]) -> str:
    """GitHub blob links; basename link text, root-stripped path on basename collision.

    Falls back to ``render_bare`` (plain ``path:line`` bullets, no link) when no
    ``github_blob_base`` is configured — see ``config.py``.
    """
    base = C.github_blob_base()
    if not base:
        return render_bare(entries)

    by_name: dict[str, set[str]] = {}                     # basename → distinct paths sharing it
    for path, _, _ in entries:
        by_name.setdefault(PurePosixPath(path).name, set()).add(path)

    prefix = C.root_prefix()
    lines = ["**Sources:**"]
    for path, a, bnd in entries:
        name = PurePosixPath(path).name
        if len(by_name[name]) > 1:                        # collision → root-stripped path
            text_path = path[len(prefix):] if prefix and path.startswith(prefix) else path
        else:
            text_path = name
        rng_text = f"{a}-{bnd}" if bnd is not None else f"{a}"
        anchor = f"#L{a}-L{bnd}" if bnd is not None else f"#L{a}"
        lines.append(f"- [{text_path}:{rng_text}]({base}{path}{anchor})")
    return "\n".join(lines)


# ------------------------------------------------------------------ prose hygiene
def strip_inline_paths(prose: str) -> tuple[str, int]:
    """Replace repo-ish inline paths (outside fences) with backticked basenames.

    Markdown TABLE rows are exempt: the verbatim reference tables (routes, env flags, …)
    legitimately carry ``path:line`` cells — rewriting them would corrupt the table and
    unfairly fail the section.
    """
    count = 0

    def _repl(m: re.Match) -> str:
        nonlocal count
        count += 1
        token = m.group(0).strip("`")
        token = token.split(":")[0]                       # drop :line suffix
        return f"`{PurePosixPath(token).name}`"

    parts = prose.split("```")
    for i in range(0, len(parts), 2):                     # even indexes = outside fences
        lines = parts[i].splitlines(keepends=True)
        for j, line in enumerate(lines):
            if line.lstrip().startswith("|"):             # table row — leave verbatim
                continue
            lines[j] = INLINE_PATH_RE.sub(_repl, line)
        parts[i] = "".join(lines)
    return "```".join(parts), count


# ------------------------------------------------------------------ final page pass
def linkify_page(conn: sqlite3.Connection, md: str) -> str:
    """Convert every remaining bare-token Sources block into GitHub links (runs LAST)."""
    lines = md.splitlines()
    out: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if not SOURCES_HEAD_RE.match(line.strip()):
            out.append(line)
            i += 1
            continue
        entries: list[str] = []
        j = i + 1
        while j < len(lines):
            m = ENTRY_RE.match(lines[j])
            if not m:
                break
            entries.append(f"{m.group(1)}:{m.group(2)}"
                           + (f"-{m.group(3)}" if m.group(3) else ""))
            j += 1
        if not entries:                                   # already linkified or empty — leave
            out.append(line)
            i += 1
            continue
        kept, _ = repair_entries(conn, entries, None)
        if kept:
            out.extend(render_sources(kept).splitlines())
        i = j
    return "\n".join(out)
