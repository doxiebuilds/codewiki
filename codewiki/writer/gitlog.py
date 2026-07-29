"""
gitlog.py — git evidence for page updates (stored-gitHead pattern, self-contained).

Each successful page build records the git HEAD it was written at; the next build of that page
feeds "what changed since" into the writer context so it pays extra attention to moved code.
Best-effort: any git failure returns an empty evidence block, never aborts a build.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from codewiki import config as C
from codewiki.paths import REPO_ROOT

_STATUS_LABEL = {"A": "added", "M": "modified", "D": "removed", "T": "modified", "R": "renamed"}


def changed_since(head: str, repo_root: Path = REPO_ROOT) -> list[tuple[str, str]]:
    """[(status, repo-relative path)] between `head` and the working tree (committed + not)."""
    if not head:
        return []
    pathspec = C.root_prefix() or "."
    out: list[tuple[str, str]] = []
    for args in (["diff", "--name-status", head, "HEAD", "--", pathspec],
                 ["diff", "--name-status", "HEAD", "--", pathspec]):
        try:
            res = subprocess.run(["git", "--no-pager", *args], cwd=str(repo_root),
                                 capture_output=True, text=True, check=True)
        except (subprocess.CalledProcessError, FileNotFoundError):
            continue
        for line in res.stdout.splitlines():
            parts = line.split("\t")
            if len(parts) < 2:
                continue
            status = _STATUS_LABEL.get(parts[0][:1], parts[0][:1])
            path = parts[-1]                       # renames: new path is last
            if (status, path) not in out:
                out.append((status, path))
    return out


def evidence_block(changes: list[tuple[str, str]], include_prefixes: list[str],
                   max_lines: int = 25) -> str:
    hits = [f"{status}: {path}" for status, path in changes
            if any(path.startswith(p) for p in include_prefixes)]
    if not hits:
        return ""
    shown = hits[:max_lines]
    if len(hits) > max_lines:
        shown.append(f"…(+{len(hits) - max_lines} more)")
    return "\n".join(shown)
