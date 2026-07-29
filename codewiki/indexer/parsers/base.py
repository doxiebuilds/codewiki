"""
base.py — common parser data model + tree-sitter helpers shared by all language extractors.

A ``Symbol`` is any documentable node (module/class/function/method). ``content_hash`` is the
sha256 of the exact source span — the leaf-level change unit that drives incremental
re-summarization. Language extractors build a nested picture but we emit a *flat* list with
``parent_qualname`` links; the graph layer resolves parents to ids.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from functools import lru_cache

from tree_sitter import Node


@dataclass
class Symbol:
    kind: str                      # module|class|function|method
    name: str
    qualname: str                  # dotted-within-file; "" for module
    start_line: int                # 1-based
    end_line: int
    signature: str = ""
    docstring: str = ""
    decorators: list[str] = field(default_factory=list)
    parent_qualname: str | None = None
    calls: list[str] = field(default_factory=list)   # raw callee names within this symbol
    content_hash: str = ""


@dataclass
class Import:
    raw: str                       # the import statement text as written
    module: str = ""               # best-effort module/path being imported
    names: list[str] = field(default_factory=list)


@dataclass
class FileParse:
    path: str
    language: str
    module_docstring: str = ""
    imports: list[Import] = field(default_factory=list)
    symbols: list[Symbol] = field(default_factory=list)


# ------------------------------------------------------------------ tree-sitter helpers
@lru_cache(maxsize=8)
def get_parser(language: str):
    """A tree_sitter.Parser for `language`, built from the language-pack grammar.

    We construct Parser(Language) ourselves rather than using the pack's get_parser(): the
    pack's returned parser rejects bytes input on tree-sitter 0.26, whereas the canonical
    Parser(lang).parse(bytes) path works.
    """
    from tree_sitter import Parser
    from tree_sitter_language_pack import get_language
    return Parser(get_language(language))


def text(node: Node, source: bytes) -> str:
    return source[node.start_byte:node.end_byte].decode("utf-8", "replace")


def line_of(node: Node) -> int:
    return node.start_point[0] + 1


def span_hash(source: bytes, node: Node) -> str:
    return hashlib.sha256(source[node.start_byte:node.end_byte]).hexdigest()


def child_by_field(node: Node, field_name: str) -> Node | None:
    return node.child_by_field_name(field_name)


def first_line(s: str, limit: int = 300) -> str:
    """Collapse a multi-line signature/docstring lead to a single trimmed line."""
    line = " ".join(s.split())
    return line[:limit]
