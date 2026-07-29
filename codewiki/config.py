"""
config.py — locates the target repo, resolves the optional source subdirectory, the GitHub
blob-link base, and (for diagrams.py) the architectural layer taxonomy.

Resolution order for every value: environment variable -> ``codewiki.toml`` at the target repo
root -> an auto-detected default (git root / `origin` remote) -> "" (feature disabled).

``codewiki.toml`` lives at the root of the repo you are INDEXING (not inside the codewiki
package). All keys are optional:

    [codewiki]
    source_subdir = "backend"          # index only this subdir of the repo (default: whole repo)
    github_blob_base = "https://github.com/you/repo/blob/main/"   # enables Sources links

    [[diagram.layers]]                 # architecture-diagram layer taxonomy (see diagrams.py)
    key = "api"
    title = "API"
    css_class = "api"
    needles = ["apps/api", "server/"]

Env var overrides: ``CODEWIKI_REPO_ROOT``, ``CODEWIKI_SOURCE_SUBDIR``, ``CODEWIKI_GITHUB_BASE``.
"""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover — repo targets 3.11+, this just avoids a hard ImportError on older runs
    tomllib = None


def _git_root(start: Path) -> Path | None:
    try:
        out = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=str(start),
                             capture_output=True, text=True, timeout=5, check=True)
        return Path(out.stdout.strip())
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        return None


def _git_origin_blob_base(repo_root: Path) -> str:
    """Best-effort ``https://github.com/org/repo/blob/main/`` derived from `origin`. "" if none."""
    try:
        out = subprocess.run(["git", "remote", "get-url", "origin"], cwd=str(repo_root),
                             capture_output=True, text=True, timeout=5, check=True)
        url = out.stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        return ""
    if url.startswith("git@github.com:"):
        org_repo = url[len("git@github.com:"):]
    elif "github.com/" in url:
        org_repo = url.split("github.com/", 1)[1]
    else:
        return ""
    org_repo = org_repo[:-len(".git")] if org_repo.endswith(".git") else org_repo
    return f"https://github.com/{org_repo}/blob/main/"


@dataclass(frozen=True)
class DiagramLayer:
    key: str
    title: str
    css_class: str
    needles: tuple[str, ...]


@dataclass(frozen=True)
class Config:
    repo_root: Path
    source_subdir: str            # "" = index the whole repo
    github_blob_base: str         # "" = Sources render as plain `path:line`, no link
    diagram_layers: tuple[DiagramLayer, ...] = ()


def _load_toml(repo_root: Path) -> dict:
    path = repo_root / "codewiki.toml"
    if not path.exists() or tomllib is None:
        return {}
    with path.open("rb") as f:
        return tomllib.load(f)


@lru_cache(maxsize=1)
def load() -> Config:
    explicit_root = os.environ.get("CODEWIKI_REPO_ROOT")
    repo_root = Path(explicit_root).resolve() if explicit_root else (
        _git_root(Path.cwd()) or Path.cwd())
    data = _load_toml(repo_root)
    cw = data.get("codewiki", {})

    env_subdir = os.environ.get("CODEWIKI_SOURCE_SUBDIR")
    source_subdir = env_subdir if env_subdir is not None else cw.get("source_subdir", "")

    github_blob_base = (os.environ.get("CODEWIKI_GITHUB_BASE")
                        or cw.get("github_blob_base")
                        or _git_origin_blob_base(repo_root))

    layers = tuple(
        DiagramLayer(key=l["key"], title=l.get("title", l["key"]),
                    css_class=l.get("css_class", l["key"]),
                    needles=tuple(l.get("needles", [])))
        for l in data.get("diagram", {}).get("layers", []))

    return Config(repo_root=repo_root, source_subdir=source_subdir.strip("/"),
                 github_blob_base=github_blob_base, diagram_layers=layers)


def reset_cache() -> None:
    """Test/CLI hook: force the next `load()` to re-resolve (cwd or env may have changed).

    Defensive against tests that monkeypatch `load` itself (e.g. to inject diagram_layers,
    which have no env-var override) with a plain callable lacking `cache_clear`.
    """
    clear = getattr(load, "cache_clear", None)
    if clear is not None:
        clear()


def rootify(path: str) -> str:
    """Prefix a repo-relative path with the configured source subdir, if any."""
    sub = load().source_subdir
    return f"{sub}/{path}" if sub else path


def strip_root(path: str) -> str:
    """Remove the configured source-subdir prefix, if present."""
    sub = load().source_subdir
    prefix = f"{sub}/"
    return path[len(prefix):] if sub and path.startswith(prefix) else path


def root_prefix() -> str:
    """The source-subdir prefix as it appears in stored paths, e.g. 'backend/' or ''."""
    sub = load().source_subdir
    return f"{sub}/" if sub else ""


def github_blob_base() -> str:
    """The configured GitHub blob-link base, or "" if Sources links are disabled."""
    return load().github_blob_base
