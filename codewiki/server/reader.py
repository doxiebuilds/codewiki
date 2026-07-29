"""
reader.py — read-only access to a generated wiki (``docs/wiki/manifest.json`` + ``<slug>.md``).

Framework-neutral: no FastAPI/async here, just plain functions over the filesystem, so any web
framework (or a CLI, or a static-site generator) can wrap them. See ``docs/OUTPUT_CONTRACT.md``
for the manifest/page shape this reads. Results are cached in-memory and invalidated by the
manifest's mtime, so repeated reads during a request burst don't re-parse JSON from disk.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from codewiki.paths import PAGE_MANIFEST, WIKI_DIR

_cache: dict[str, Any] = {"mtime": None, "manifest": None}


def load_manifest() -> dict[str, Any]:
    """The manifest dict, cached and invalidated by manifest.json's mtime.

    Degrades to ``{"available": False, ...}`` (never raises) when the wiki hasn't been
    generated yet or the manifest is corrupt — callers can render that as an empty state.
    """
    if not PAGE_MANIFEST.is_file():
        return {"available": False, "pages": [], "page_count": 0}
    mtime = PAGE_MANIFEST.stat().st_mtime
    if _cache["mtime"] == mtime and _cache["manifest"] is not None:
        return _cache["manifest"]
    try:
        data = json.loads(PAGE_MANIFEST.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        return {"available": False, "pages": [], "page_count": 0, "error": str(exc)}
    data["available"] = True
    _cache["mtime"] = mtime
    _cache["manifest"] = data
    return data


def read_page(slug: str) -> dict[str, Any] | None:
    """One page's raw markdown + metadata, or None if the slug is unknown."""
    manifest = load_manifest()
    entry = next((p for p in manifest.get("pages", []) if p.get("slug") == slug), None)
    if entry is None:
        return None
    md_path = WIKI_DIR / entry.get("file", f"{slug}.md")
    # Defend against path traversal — the resolved path must stay inside WIKI_DIR.
    if not md_path.resolve().is_relative_to(WIKI_DIR.resolve()) or not md_path.is_file():
        return None
    return {
        "slug": slug,
        "title": entry.get("title", slug),
        "summary": entry.get("summary", ""),
        "source_refs": entry.get("source_refs", []),
        "markdown": md_path.read_text(encoding="utf-8"),
        "written_at": entry.get("written_at"),
        "generated_at": manifest.get("generated_at"),
        "model": manifest.get("model"),
    }


def search(query: str, limit: int = 20) -> list[dict[str, Any]]:
    """Substring search over page titles/bodies."""
    q = query.strip().lower()
    if not q:
        return []
    hits: list[dict[str, Any]] = []
    manifest = load_manifest()
    for entry in manifest.get("pages", []):
        md_path = WIKI_DIR / entry.get("file", f"{entry['slug']}.md")
        if not md_path.is_file():
            continue
        text = md_path.read_text(encoding="utf-8")
        low = text.lower()
        if q in entry.get("title", "").lower() or q in low:
            idx = low.find(q)
            start = max(0, idx - 80)
            snippet = text[start:idx + 160].replace("\n", " ").strip()
            hits.append({"slug": entry["slug"], "title": entry.get("title", entry["slug"]),
                        "snippet": snippet})
        if len(hits) >= limit:
            break
    return hits


def refresh_status_path() -> Path:
    from codewiki.paths import STATE_DIR
    return STATE_DIR / "refresh_status.json"


def load_refresh_status() -> dict[str, Any]:
    """Latest refresh status snapshot written by ``codewiki update --status-file ...``.

    ``{"state": "idle"}`` when no refresh has ever run (or the status file is corrupt).
    """
    try:
        data = json.loads(refresh_status_path().read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"state": "idle"}
    return data if isinstance(data, dict) else {"state": "idle"}
