"""
paths.py — repo roots + include/exclude policy for codewiki (no LLM).

Everything downstream resolves paths against ``REPO_ROOT`` (the target repo's git root, from
``config.load()``) so the pipeline runs identically from any cwd inside that repo. When
``source_subdir`` is configured, only that subdir is scanned (``PROJECT_ROOT``); output still
lives under the repo root's ``docs/`` regardless, so switching subdirs doesn't move existing
state.
"""

from __future__ import annotations

import re
from pathlib import Path

from codewiki import config as C

_CFG = C.load()
REPO_ROOT = _CFG.repo_root
PROJECT_ROOT = (REPO_ROOT / _CFG.source_subdir) if _CFG.source_subdir else REPO_ROOT  # the codebase we index

DOCS_DIR = REPO_ROOT / "docs"
WIKI_DIR = DOCS_DIR / "wiki"                    # output contract: <slug>.md + manifest.json
PAGE_MANIFEST = WIKI_DIR / "manifest.json"
STATE_DIR = DOCS_DIR / ".codewiki_state"        # code-graph DB lives here
GRAPH_DB = STATE_DIR / "codegraph.db"

# Keep this extension set and indexer/parsers/ in agreement so page source_refs stay resolvable.
INCLUDE_EXTS = {".py", ".rs", ".ts", ".tsx", ".js", ".jsx", ".sh", ".yml", ".yaml", ".toml", ".json", ".sql"}
# Languages we build a real symbol graph for. Others are indexed at file level only.
PARSED_LANGS = {"python", "rust", "typescript", "javascript"}

EXCLUDE_DIRS = {
    ".git", ".github", ".cache", ".mypy_cache", ".next", ".pytest_cache", ".ruff_cache", ".turbo",
    ".venv", "__pycache__", "build", "dist", "docs", "env", "logs", "node_modules", "target",
    "venv",
}
EXCLUDE_FILES = {"Cargo.lock", "package-lock.json", "pnpm-lock.yaml", "yarn.lock"}
LANG_BY_EXT = {
    ".py": "python", ".rs": "rust", ".ts": "typescript", ".tsx": "typescript", ".js": "javascript",
    ".jsx": "javascript", ".sh": "shell", ".yml": "yaml", ".yaml": "yaml", ".toml": "toml",
    ".json": "json", ".sql": "sql",
}


# Test/bench files rank high by symbol count but make poor page evidence — a test suite churns
# through more assertions than the code under test has real logic, so bundles built from raw
# symbol counts skew toward tests instead of the thing being tested. De-prioritize them except
# on pages that opt in via `keep_tests` (a page that IS about the tests).
TEST_PATH_RE = re.compile(
    r"(^|/)(tests?|benchmarks|benches)(/|$)"
    r"|(^|/)(test_[^/]+|[^/]+_test)\.(py|rs|ts|tsx|js|jsx)$"
    r"|(^|/)conftest\.py$")


def is_test_path(path: str) -> bool:
    return bool(TEST_PATH_RE.search(path))


def iter_repo_files(root: Path, suffixes: set[str] | None = None):
    """Walk `root` yielding files, PRUNING excluded dirs during the walk.

    ``rglob`` visits every directory before the exclude filter runs, which is slow on repos with
    huge data/vendor trees. ``os.walk`` with in-place ``dirnames`` pruning never descends into
    excluded directories at all.
    """
    import os
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in EXCLUDE_DIRS)
        for name in sorted(filenames):
            if suffixes is not None and Path(name).suffix not in suffixes:
                continue
            yield Path(dirpath) / name


def rel_to_repo(path: Path) -> str:
    """Repo-root-relative POSIX path, e.g. ``backend/apps/.../main.py`` (subdir if configured)."""
    return path.resolve().relative_to(REPO_ROOT).as_posix()


def should_include(path: Path) -> bool:
    if path.name in EXCLUDE_FILES:
        return False
    if path.suffix not in INCLUDE_EXTS:
        return False
    return not any(part in EXCLUDE_DIRS for part in path.parts)


def language_of(path: Path) -> str:
    return LANG_BY_EXT.get(path.suffix, path.suffix.lstrip("."))
