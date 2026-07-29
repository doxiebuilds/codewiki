"""
discovery.py — walk the codebase and yield file metadata (deterministic; no LLM).

Applies ``paths.should_include`` while walking, so vendor and virtualenv trees (``.venv``,
``node_modules``, ...) never reach the parser. Emits repo-root-relative paths and a content
sha256 — the sha is the file-level change signal that gates whether we re-parse a file at all.
"""

from __future__ import annotations

import hashlib
import subprocess
from dataclasses import dataclass
from pathlib import Path

from codewiki.paths import (PROJECT_ROOT, REPO_ROOT, iter_repo_files, language_of, rel_to_repo,
                            should_include)


@dataclass
class FileMeta:
    path: str          # repo-root-relative POSIX
    abs: Path
    language: str
    sha256: str
    size: int


def discover() -> list[FileMeta]:
    out: list[FileMeta] = []
    for path in iter_repo_files(PROJECT_ROOT):
        if not should_include(path):
            continue
        try:
            raw = path.read_bytes()
            raw.decode("utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        out.append(FileMeta(
            path=rel_to_repo(path), abs=path, language=language_of(path),
            sha256=hashlib.sha256(raw).hexdigest(), size=len(raw),
        ))
    return out


def head_sha() -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=str(REPO_ROOT),
            capture_output=True, text=True, check=True,
        ).stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return ""
