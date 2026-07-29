"""
typescript.py — tree-sitter TS/TSX/JS/JSX extractor (uses the ``tsx`` grammar, a superset).

Emits a module Symbol plus function declarations, arrow/function consts, classes (with their
method_definitions), and interfaces/type aliases (as ``class``-kind nodes). Leading ``/** */``
JSDoc becomes the docstring. ``export`` wrappers are unwrapped to the inner declaration.
"""

from __future__ import annotations

from tree_sitter import Node

from codewiki.indexer.parsers.base import (
    FileParse, Import, Symbol, child_by_field, first_line, get_parser, line_of, span_hash, text,
)

_FUNC_VALUE = {"arrow_function", "function", "function_expression"}


def parse(path: str, source: bytes) -> FileParse:
    tree = get_parser("tsx").parse(source)
    root = tree.root_node
    fp = FileParse(path=path, language="typescript")
    fp.imports = _imports(root, source)

    symbols: list[Symbol] = [Symbol(
        kind="module", name=path.rsplit("/", 1)[-1], qualname="",
        start_line=1, end_line=(root.end_point[0] + 1), content_hash=span_hash(source, root),
    )]
    for child in root.named_children:
        _walk(child, source, out=symbols)
    fp.symbols = symbols
    return fp


def _unwrap_export(node: Node) -> Node:
    if node.type == "export_statement":
        decl = node.child_by_field_name("declaration")
        if decl is not None:
            return decl
        for c in node.named_children:
            if c.type not in {"comment"}:
                return c
    return node


def _walk(node: Node, source: bytes, *, out: list[Symbol]) -> None:
    node = _unwrap_export(node)
    t = node.type

    if t == "function_declaration":
        _emit_function(node, source, out)
    elif t in {"class_declaration", "abstract_class_declaration"}:
        _emit_class(node, source, out)
    elif t in {"interface_declaration", "type_alias_declaration", "enum_declaration"}:
        _emit_typelike(node, source, out)
    elif t in {"lexical_declaration", "variable_declaration"}:
        _emit_var_functions(node, source, out)


def _emit_function(node: Node, source: bytes, out: list[Symbol]) -> None:
    name_node = child_by_field(node, "name")
    if name_node is None:
        return
    name = text(name_node, source)
    out.append(Symbol(
        kind="function", name=name, qualname=name,
        start_line=line_of(node), end_line=(node.end_point[0] + 1),
        signature=_fn_signature(node, name, source), docstring=_leading_doc(node, source),
        calls=_direct_calls(child_by_field(node, "body"), source),
        content_hash=span_hash(source, node),
    ))


def _emit_class(node: Node, source: bytes, out: list[Symbol]) -> None:
    name_node = child_by_field(node, "name")
    name = text(name_node, source) if name_node else "(anonymous)"
    out.append(Symbol(
        kind="class", name=name, qualname=name,
        start_line=line_of(node), end_line=(node.end_point[0] + 1),
        signature=first_line(f"class {name}"), docstring=_leading_doc(node, source),
        content_hash=span_hash(source, node),
    ))
    body = child_by_field(node, "body")
    if body is None:
        return
    for member in body.named_children:
        if member.type != "method_definition":
            continue
        mname_node = child_by_field(member, "name")
        if mname_node is None:
            continue
        mname = text(mname_node, source)
        out.append(Symbol(
            kind="method", name=mname, qualname=f"{name}.{mname}",
            start_line=line_of(member), end_line=(member.end_point[0] + 1),
            signature=_fn_signature(member, mname, source), docstring=_leading_doc(member, source),
            parent_qualname=name, calls=_direct_calls(child_by_field(member, "body"), source),
            content_hash=span_hash(source, member),
        ))


def _emit_typelike(node: Node, source: bytes, out: list[Symbol]) -> None:
    name_node = child_by_field(node, "name")
    if name_node is None:
        return
    name = text(name_node, source)
    keyword = {"interface_declaration": "interface", "type_alias_declaration": "type",
               "enum_declaration": "enum"}.get(node.type, "type")
    out.append(Symbol(
        kind="class", name=name, qualname=name,
        start_line=line_of(node), end_line=(node.end_point[0] + 1),
        signature=first_line(f"{keyword} {name}"), docstring=_leading_doc(node, source),
        content_hash=span_hash(source, node),
    ))


def _emit_var_functions(node: Node, source: bytes, out: list[Symbol]) -> None:
    for declr in node.named_children:
        if declr.type != "variable_declarator":
            continue
        value = child_by_field(declr, "value")
        name_node = child_by_field(declr, "name")
        if value is None or name_node is None or value.type not in _FUNC_VALUE:
            continue
        name = text(name_node, source)
        out.append(Symbol(
            kind="function", name=name, qualname=name,
            start_line=line_of(node), end_line=(declr.end_point[0] + 1),
            signature=_arrow_signature(name, value, source), docstring=_leading_doc(node, source),
            calls=_direct_calls(child_by_field(value, "body"), source),
            content_hash=span_hash(source, declr),
        ))


def _fn_signature(node: Node, name: str, source: bytes) -> str:
    params = child_by_field(node, "parameters")
    ret = child_by_field(node, "return_type")
    sig = f"{name}{text(params, source) if params else '()'}"
    if ret:
        sig += text(ret, source)
    return first_line(sig)


def _arrow_signature(name: str, value: Node, source: bytes) -> str:
    params = child_by_field(value, "parameters")
    ret = child_by_field(value, "return_type")
    sig = f"const {name} = {text(params, source) if params else '()'} =>"
    if ret:
        sig = f"const {name} = {text(params, source) if params else '()'}{text(ret, source)} =>"
    return first_line(sig)


def _direct_calls(body: Node | None, source: bytes) -> list[str]:
    if body is None:
        return []
    found: list[str] = []

    def visit(n: Node) -> None:
        for c in n.named_children:
            if c.type in {"function_declaration", "class_declaration", "method_definition",
                          "arrow_function", "function", "function_expression"}:
                continue
            if c.type == "call_expression":
                fn = c.child_by_field_name("function")
                if fn is not None:
                    found.append(text(fn, source).split(".")[-1].split("(", 1)[0])
            visit(c)

    visit(body)
    seen: set[str] = set()
    out = []
    for n in found:
        n = n.strip()
        if n and n not in seen:
            seen.add(n)
            out.append(n)
    return out


def _leading_doc(node: Node, source: bytes) -> str:
    sib = node.prev_sibling
    # skip past an export keyword sibling if present
    while sib is not None and sib.type == "comment":
        raw = text(sib, source).strip()
        if raw.startswith("/**"):
            cleaned = raw.strip("/").strip("*").replace("*", " ")
            return first_line(cleaned, limit=400)
        sib = sib.prev_sibling
    return ""


def _imports(root: Node, source: bytes) -> list[Import]:
    out: list[Import] = []
    for child in root.named_children:
        node = child
        if child.type == "export_statement":
            node = _unwrap_export(child)
        if node.type == "import_statement":
            src = child_by_field(node, "source")
            out.append(Import(raw=first_line(text(node, source)),
                              module=text(src, source).strip("'\"") if src else ""))
    return out
