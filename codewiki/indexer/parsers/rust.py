"""
rust.py — tree-sitter Rust extractor.

Emits a module Symbol plus functions, structs, enums, traits, impl blocks (their methods become
``Type::method`` symbols), and nested ``mod`` items. Doc comments (`///`, `//!`, `/** */`) that
immediately precede an item become its docstring. Rust repos tend to be smaller than their
Python/TS counterparts in a mixed codebase, so this favours clarity over exhaustive coverage.
"""

from __future__ import annotations

from tree_sitter import Node

from codewiki.indexer.parsers.base import (
    FileParse, Import, Symbol, child_by_field, first_line, get_parser, line_of, span_hash, text,
)

_ITEM_NODES = {"function_item", "struct_item", "enum_item", "union_item", "trait_item",
               "impl_item", "mod_item"}


def parse(path: str, source: bytes) -> FileParse:
    tree = get_parser("rust").parse(source)
    root = tree.root_node
    fp = FileParse(path=path, language="rust")
    fp.imports = _imports(root, source)

    symbols: list[Symbol] = [Symbol(
        kind="module", name=path.rsplit("/", 1)[-1], qualname="",
        start_line=1, end_line=(root.end_point[0] + 1),
        docstring=_inner_doc(root, source), content_hash=span_hash(source, root),
    )]
    for child in root.named_children:
        _walk_item(child, source, parent_qual="", parent_kind="module", out=symbols)
    fp.symbols = symbols
    return fp


def _walk_item(node: Node, source: bytes, *, parent_qual: str, parent_kind: str,
               out: list[Symbol]) -> None:
    if node.type not in _ITEM_NODES:
        return

    if node.type == "impl_item":
        _walk_impl(node, source, parent_qual=parent_qual, out=out)
        return

    name_node = child_by_field(node, "name")
    name = text(name_node, source) if name_node else _fallback_name(node, source)
    if not name:
        return
    qual = f"{parent_qual}::{name}" if parent_qual else name
    kind, sig = _kind_and_signature(node, name, source)
    body = child_by_field(node, "body")
    out.append(Symbol(
        kind=kind, name=name, qualname=qual,
        start_line=line_of(node), end_line=(node.end_point[0] + 1),
        signature=sig, docstring=_leading_doc(node, source),
        decorators=_attributes(node, source),
        parent_qualname=parent_qual or None,
        calls=_direct_calls(body, source) if body else [],
        content_hash=span_hash(source, node),
    ))
    if node.type == "mod_item" and body is not None:
        for child in body.named_children:
            _walk_item(child, source, parent_qual=qual, parent_kind="module", out=out)


def _walk_impl(node: Node, source: bytes, *, parent_qual: str, out: list[Symbol]) -> None:
    type_node = child_by_field(node, "type")
    type_name = text(type_node, source) if type_node else "impl"
    trait_node = child_by_field(node, "trait")
    label = f"{text(trait_node, source)} for {type_name}" if trait_node else type_name
    impl_qual = f"{parent_qual}::{type_name}" if parent_qual else type_name
    out.append(Symbol(
        kind="class", name=type_name, qualname=impl_qual,
        start_line=line_of(node), end_line=(node.end_point[0] + 1),
        signature=first_line(f"impl {label}"), docstring=_leading_doc(node, source),
        decorators=_attributes(node, source),
        parent_qualname=parent_qual or None, content_hash=span_hash(source, node),
    ))
    body = child_by_field(node, "body")
    if body is None:
        return
    for child in body.named_children:
        if child.type != "function_item":
            continue
        name_node = child_by_field(child, "name")
        if name_node is None:
            continue
        mname = text(name_node, source)
        mbody = child_by_field(child, "body")
        out.append(Symbol(
            kind="method", name=mname, qualname=f"{impl_qual}::{mname}",
            start_line=line_of(child), end_line=(child.end_point[0] + 1),
            signature=_fn_signature(child, mname, source), docstring=_leading_doc(child, source),
            decorators=_attributes(child, source),
            parent_qualname=impl_qual, calls=_direct_calls(mbody, source) if mbody else [],
            content_hash=span_hash(source, child),
        ))


def _kind_and_signature(node: Node, name: str, source: bytes) -> tuple[str, str]:
    t = node.type
    if t == "function_item":
        return "function", _fn_signature(node, name, source)
    if t == "struct_item":
        return "class", first_line(f"struct {name}")
    if t == "enum_item":
        return "class", first_line(f"enum {name}")
    if t == "union_item":
        return "class", first_line(f"union {name}")
    if t == "trait_item":
        return "class", first_line(f"trait {name}")
    if t == "mod_item":
        return "module", first_line(f"mod {name}")
    return "function", first_line(name)


def _fn_signature(node: Node, name: str, source: bytes) -> str:
    params = child_by_field(node, "parameters")
    ret = child_by_field(node, "return_type")
    sig = f"fn {name}{text(params, source) if params else '()'}"
    if ret:
        sig += f" -> {text(ret, source)}"
    return first_line(sig)


def _fallback_name(node: Node, source: bytes) -> str:
    nn = child_by_field(node, "name")
    return text(nn, source) if nn else ""


def _direct_calls(body: Node, source: bytes) -> list[str]:
    found: list[str] = []

    def visit(n: Node) -> None:
        for c in n.named_children:
            if c.type in {"function_item", "impl_item", "mod_item", "struct_item", "trait_item"}:
                continue
            if c.type == "call_expression":
                fn = c.child_by_field_name("function")
                if fn is not None:
                    found.append(text(fn, source).split("::")[-1].split(".")[-1])
            elif c.type == "macro_invocation":
                mac = c.child_by_field_name("macro")
                if mac is not None:
                    found.append(text(mac, source).split("::")[-1] + "!")
            visit(c)

    visit(body)
    seen: set[str] = set()
    out = []
    for n in found:
        if n and n not in seen:
            seen.add(n)
            out.append(n)
    return out


def _leading_doc(node: Node, source: bytes) -> str:
    """Concatenate `///` / `/** */` doc comments immediately preceding `node`.

    Attribute items (`#[pyfunction]` etc.) sit between the doc comment and the item —
    skip over them so an attributed item keeps its doc.
    """
    lines: list[str] = []
    sib = node.prev_sibling
    while sib is not None and sib.type in {"line_comment", "block_comment", "attribute_item"}:
        if sib.type != "attribute_item":
            raw = text(sib, source).strip()
            if raw.startswith("///") or raw.startswith("//!") or raw.startswith("/**"):
                cleaned = raw.lstrip("/").lstrip("!").lstrip("*").strip().rstrip("*/").strip()
                lines.insert(0, cleaned)
        sib = sib.prev_sibling
    return first_line(" ".join(l for l in lines if l), limit=400)


def _attributes(node: Node, source: bytes) -> list[str]:
    """`#[...]` attribute texts immediately preceding `node` (stripped), e.g. `pyfunction`."""
    attrs: list[str] = []
    sib = node.prev_sibling
    while sib is not None and sib.type in {"line_comment", "block_comment", "attribute_item"}:
        if sib.type == "attribute_item":
            raw = text(sib, source).strip()
            attrs.insert(0, first_line(raw.removeprefix("#[").removesuffix("]").strip(), limit=120))
        sib = sib.prev_sibling
    return attrs


def _inner_doc(root: Node, source: bytes) -> str:
    for child in root.named_children:
        if child.type == "line_comment" and text(child, source).strip().startswith("//!"):
            return first_line(text(child, source).strip().lstrip("/").lstrip("!").strip(), limit=400)
    return ""


def _imports(root: Node, source: bytes) -> list[Import]:
    out: list[Import] = []
    for child in root.named_children:
        if child.type == "use_declaration":
            out.append(Import(raw=first_line(text(child, source))))
    return out
