"""
python.py — tree-sitter Python extractor.

Emits a module Symbol plus one Symbol per top-level/nested function and class (methods included),
with signatures, docstrings, decorators, and the raw call names made *directly* inside each symbol
(calls inside a nested def/class belong to that child, not the parent).
"""

from __future__ import annotations

from tree_sitter import Node

from codewiki.indexer.parsers.base import (
    FileParse, Import, Symbol, child_by_field, first_line, get_parser, line_of, span_hash, text,
)

_DEF_NODES = {"function_definition", "class_definition", "decorated_definition"}


def parse(path: str, source: bytes) -> FileParse:
    tree = get_parser("python").parse(source)
    root = tree.root_node
    fp = FileParse(path=path, language="python")

    fp.module_docstring = _docstring_of_block(root, source)
    fp.imports = _imports(root, source)

    symbols: list[Symbol] = []
    # module symbol: span-hash the whole file so file-level edits still bubble up.
    symbols.append(Symbol(
        kind="module", name=path.rsplit("/", 1)[-1], qualname="",
        start_line=1, end_line=(root.end_point[0] + 1),
        docstring=fp.module_docstring, content_hash=span_hash(source, root),
    ))
    for child in root.named_children:
        _walk_def(child, source, parent_qual="", parent_kind="module", out=symbols)
    fp.symbols = symbols
    return fp


def _unwrap_decorated(node: Node) -> tuple[Node, list[str]]:
    """Return (inner def/class node, decorator strings) for a possibly-decorated definition.

    Node.text carries that node's own source bytes, so decorators read straight off it.
    """
    if node.type != "decorated_definition":
        return node, []
    decorators = []
    for c in node.named_children:
        if c.type == "decorator":
            raw = (c.text or b"").decode("utf-8", "replace")
            decorators.append(first_line(raw.lstrip("@").strip()))
    inner = node.child_by_field_name("definition") or next(
        (c for c in node.named_children if c.type in {"function_definition", "class_definition"}), node)
    return inner, decorators


def _walk_def(node: Node, source: bytes, *, parent_qual: str, parent_kind: str,
              out: list[Symbol]) -> None:
    if node.type not in _DEF_NODES:
        return
    inner, decorators = _unwrap_decorated(node)
    if inner.type not in {"function_definition", "class_definition"}:
        return
    name_node = child_by_field(inner, "name")
    if name_node is None:
        return
    name = text(name_node, source)
    qual = f"{parent_qual}.{name}" if parent_qual else name

    if inner.type == "class_definition":
        kind = "class"
        sig = _class_signature(inner, source)
    else:
        kind = "method" if parent_kind == "class" else "function"
        sig = _func_signature(inner, source)

    body = child_by_field(inner, "body")
    sym = Symbol(
        kind=kind, name=name, qualname=qual,
        start_line=line_of(node), end_line=(inner.end_point[0] + 1),
        signature=sig, docstring=_docstring_of_block(body, source) if body else "",
        decorators=decorators, parent_qualname=parent_qual or None,
        calls=_direct_calls(body, source) if body else [],
        content_hash=span_hash(source, node),
    )
    out.append(sym)

    if body is not None:
        for child in body.named_children:
            _walk_def(child, source, parent_qual=qual, parent_kind=kind, out=out)


def _func_signature(node: Node, source: bytes) -> str:
    name = text(child_by_field(node, "name"), source) if child_by_field(node, "name") else ""
    params = child_by_field(node, "parameters")
    ret = child_by_field(node, "return_type")
    prefix = "async def" if any(c.type == "async" for c in node.children) else "def"
    sig = f"{prefix} {name}{text(params, source) if params else '()'}"
    if ret:
        sig += f" -> {text(ret, source)}"
    return first_line(sig)


def _class_signature(node: Node, source: bytes) -> str:
    name = text(child_by_field(node, "name"), source) if child_by_field(node, "name") else ""
    bases = child_by_field(node, "superclasses")
    return first_line(f"class {name}{text(bases, source) if bases else ''}")


def _docstring_of_block(block: Node, source: bytes) -> str:
    """First string literal in a block/module → its text (quotes stripped, trimmed).

    tree-sitter-python emits a module/def docstring either as a bare ``string`` node or wrapped in
    an ``expression_statement``; handle both. Only the first statement can be a docstring.
    """
    if block is None:
        return ""
    for child in block.named_children:
        if child.type == "comment":
            continue
        node = None
        if child.type == "string":
            node = child
        elif child.type == "expression_statement" and child.named_children \
                and child.named_children[0].type == "string":
            node = child.named_children[0]
        if node is not None:
            return first_line(_strip_pystring(text(node, source)), limit=400)
        break  # first non-comment statement wasn't a string → no docstring
    return ""


def _strip_pystring(raw: str) -> str:
    s = raw.strip()
    for q in ('"""', "'''", '"', "'"):
        if s.startswith(q) and s.endswith(q) and len(s) >= 2 * len(q):
            return s[len(q):-len(q)].strip()
    # strip leading string prefixes like r, b, f
    return s.lstrip("rbfRBF").strip("\"'").strip()


def _direct_calls(body: Node, source: bytes) -> list[str]:
    """Callee names invoked directly in `body`, not descending into nested def/class."""
    found: list[str] = []

    def visit(n: Node) -> None:
        for c in n.named_children:
            if c.type in {"function_definition", "class_definition", "decorated_definition"}:
                continue  # belongs to the child symbol
            if c.type == "call":
                fn = c.child_by_field_name("function")
                if fn is not None:
                    found.append(_callee_name(text(fn, source)))
            visit(c)

    visit(body)
    # de-dup preserving order
    seen: set[str] = set()
    ordered = []
    for name in found:
        if name and name not in seen:
            seen.add(name)
            ordered.append(name)
    return ordered


def _callee_name(raw: str) -> str:
    """`self.foo.bar(...)` → `bar`; `redis.publish` → `publish`; `func` → `func`."""
    head = raw.split("(", 1)[0].strip()
    return head.split(".")[-1] if head else ""


def _imports(root: Node, source: bytes) -> list[Import]:
    imports: list[Import] = []
    for child in root.named_children:
        if child.type == "import_statement":
            imports.append(Import(raw=first_line(text(child, source)),
                                  names=[text(c, source) for c in child.named_children]))
        elif child.type == "import_from_statement":
            mod = child.child_by_field_name("module_name")
            names = [text(c, source) for c in child.named_children if c is not mod]
            imports.append(Import(raw=first_line(text(child, source)),
                                  module=text(mod, source) if mod else "", names=names))
    return imports
