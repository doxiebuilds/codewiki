"""
parsers — normalise Python/Rust/TS/JS source into one ``FileParse`` shape via tree-sitter.

``parse_source(path, language, source)`` dispatches to the per-language extractor and returns a
``FileParse`` (module docstring, imports, and a flat list of ``Symbol`` with byte/line spans,
signatures, docstrings, decorators, and raw call names). Parsers never raise on malformed input:
tree-sitter yields ERROR nodes rather than throwing, and any per-file exception is swallowed by
the caller so one bad file can't abort a full index.
"""

from __future__ import annotations

from codewiki.indexer.parsers.base import FileParse, Import, Symbol

_EXTRACTORS = {}


def _load():
    if _EXTRACTORS:
        return _EXTRACTORS
    from codewiki.indexer.parsers import python, rust, typescript
    _EXTRACTORS["python"] = python.parse
    _EXTRACTORS["rust"] = rust.parse
    _EXTRACTORS["typescript"] = typescript.parse
    _EXTRACTORS["javascript"] = typescript.parse  # tree-sitter tsx grammar handles JS/JSX too
    return _EXTRACTORS


def parse_source(path: str, language: str, source: bytes) -> FileParse | None:
    """Return a FileParse for a parsed language, or None for file-level-only languages."""
    extractor = _load().get(language)
    if extractor is None:
        return None
    return extractor(path, source)


__all__ = ["FileParse", "Import", "Symbol", "parse_source"]
