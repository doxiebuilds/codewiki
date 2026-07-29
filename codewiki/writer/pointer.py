"""
pointer.py — keep an idempotent "read the wiki first" section in the target repo's root
CLAUDE.md, if one exists (a no-op otherwise — this never creates the file). Marker-delimited so
updates replace, never duplicate; skipped entirely when the section is already current (no
formatting-only edits).
"""

from __future__ import annotations

from pathlib import Path

from codewiki.paths import REPO_ROOT, WIKI_DIR

BEGIN = "<!-- codewiki-pointer:begin -->"
END = "<!-- codewiki-pointer:end -->"


def _section() -> str:
    rel = WIKI_DIR.relative_to(REPO_ROOT).as_posix()
    return f"""{BEGIN}
## Generated wiki (read first for architecture context)

This repository has a generated, citation-grounded wiki at
[{rel}/quickstart.md]({rel}/quickstart.md). It covers architecture, data flow, key interfaces,
storage, and per-area watch-outs — with `path:line` citations computed from the code graph.

When working in this repository, skim the quickstart first, then follow its links to the page
covering the area you are changing. Regenerate after structural changes with `codewiki update`.
{END}"""


def ensure_pointer_section(claude_md: Path = REPO_ROOT / "CLAUDE.md") -> bool:
    """Insert or refresh the pointer section. Returns True iff the file was modified."""
    if not claude_md.exists():
        return False
    section = _section()
    text = claude_md.read_text(encoding="utf-8")
    if BEGIN in text and END in text:
        start, end = text.index(BEGIN), text.index(END) + len(END)
        if text[start:end] == section:
            return False
        new = text[:start] + section + text[end:]
    else:
        new = text.rstrip() + "\n\n" + section + "\n"
    claude_md.write_text(new, encoding="utf-8")
    return True
