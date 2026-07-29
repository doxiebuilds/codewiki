"""Parser extraction + content-hash stability (the incremental change unit)."""

from codewiki.indexer.parsers import parse_source

PY = b'''"""Module doc."""
import os
from a.b import c

def alpha(x: int) -> int:
    """Alpha doc."""
    return helper(x)

class Foo(Base):
    def method(self, y):
        return alpha(y)
'''

RS = b'''//! crate doc
use std::sync::Arc;

/// A widget.
pub struct Widget { n: u32 }

impl Widget {
    /// make one
    pub fn new(n: u32) -> Self { Widget { n } }
}
'''

JS = b'''import {useState} from 'react';

/** A hook. */
export function useThing(x) {
  const [s, setS] = useState(0);
  return doWork(s);
}

export class Panel {
  render() { return build(); }
}
'''


def _by_qual(fp):
    return {s.qualname or s.name: s for s in fp.symbols}


def test_python_symbols_and_edges():
    fp = parse_source("pkg/mod.py", "python", PY)
    q = _by_qual(fp)
    assert fp.module_docstring.startswith("Module doc")
    assert "alpha" in q and q["alpha"].kind == "function"
    assert q["alpha"].docstring.startswith("Alpha doc")
    assert "helper" in q["alpha"].calls
    assert q["Foo"].kind == "class"
    assert q["Foo.method"].kind == "method"
    assert "alpha" in q["Foo.method"].calls
    assert any(i.module == "a.b" for i in fp.imports)


def test_rust_symbols():
    fp = parse_source("src/w.rs", "rust", RS)
    q = _by_qual(fp)
    assert "Widget" in q  # struct
    assert any(s.kind == "method" and s.name == "new" for s in fp.symbols)


def test_typescript_symbols():
    fp = parse_source("app.tsx", "typescript", JS)
    q = _by_qual(fp)
    assert "useThing" in q and q["useThing"].kind == "function"
    assert "doWork" in q["useThing"].calls
    assert "Panel" in q and q["Panel"].kind == "class"


def test_content_hash_is_stable_and_local():
    """Same source → same hashes; editing one body changes only that symbol's hash."""
    a = _by_qual(parse_source("m.py", "python", PY))
    b = _by_qual(parse_source("m.py", "python", PY))
    assert a["alpha"].content_hash == b["alpha"].content_hash

    edited = PY.replace(b"return helper(x)", b"return helper(x) + 1")
    c = _by_qual(parse_source("m.py", "python", edited))
    assert c["alpha"].content_hash != a["alpha"].content_hash          # changed body
    assert c["Foo.method"].content_hash == a["Foo.method"].content_hash  # untouched
