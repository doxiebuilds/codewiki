"""
Assembled pages carry real, resolvable citations and the expected manifest shape — no hallucinated
paths. Built on a synthetic temp graph so it never touches the real docs/wiki.
"""

import hashlib
import json
import re

from codewiki.assembly import render
from codewiki.assembly.pages import PageSpec
from codewiki.generator import summarize as S
from codewiki.indexer.discovery import FileMeta
from codewiki.indexer import graph
from codewiki.store import db

SRC = b'''"""orders module."""

def submit_order(o):
    """Submit an order."""
    return validate(o)

class OrderBook:
    def add(self, level):
        return 1
'''

REL = "repo/pkg/orders.py"
CITE_RE = re.compile(r"`(repo/[\w./-]+):(\d+)`")


def _seed(tmp_path):
    conn = db.connect(tmp_path / "g.db")
    p = tmp_path / "orders.py"
    p.write_bytes(SRC)
    fm = FileMeta(path=REL, abs=p, language="python",
                  sha256=hashlib.sha256(SRC).hexdigest(), size=len(SRC))
    graph.index_file(conn, fm)
    # a domain route rooted in the same package so the reference table renders
    db.replace_domain_nodes(conn, "route", [{
        "id": "route::GET /api/orders", "kind": "route", "name": "/api/orders",
        "detail": json.dumps({"method": "GET", "func": "list_orders"}),
        "file_path": REL, "line": 3}])
    conn.commit()
    stub = lambda prompt, *, model=None, **kw: ('{"summary": "does a thing"}',
                                                {"prompt_tokens": 1, "completion_tokens": 1})
    S.summarize_all(conn, chat_fn=stub, only_stale=True)
    return conn


def test_build_page_has_resolvable_citations(tmp_path):
    conn = _seed(tmp_path)
    spec = PageSpec(slug="orders", title="Orders", order=1,
                    include=["repo/pkg"], domain=["route"])
    page = render.build_page(conn, spec, chat_fn=None, model="")
    md = page["_markdown"]

    # every path:line citation points at a file/line that actually exists in the graph
    cites = CITE_RE.findall(md)
    assert cites, "expected at least one path:line citation"
    for path, line in cites:
        row = conn.execute("SELECT 1 FROM files WHERE path=?", (path,)).fetchone()
        assert row, f"citation path not in graph: {path}"
        assert int(line) >= 1

    # domain route table rendered
    assert "/api/orders" in md and "GET" in md
    # manifest fields present and source_refs are real files
    assert page["id"] == "01-orders" and page["slug"] == "orders"
    for ref in page["source_refs"]:
        assert conn.execute("SELECT 1 FROM files WHERE path=?", (ref,)).fetchone()


def test_assemble_writes_manifest_and_pages(tmp_path, monkeypatch):
    conn = _seed(tmp_path)
    wiki_dir = tmp_path / "wiki"
    monkeypatch.setattr(render, "WIKI_DIR", wiki_dir)
    monkeypatch.setattr(render, "PAGE_MANIFEST", wiki_dir / "manifest.json")
    # single-page taxonomy pointing at the synthetic package
    monkeypatch.setattr(render, "load_pages", lambda: [PageSpec(
        slug="orders", title="Orders", order=1,
        include=["repo/pkg"], domain=["route"])])

    manifest = render.assemble(conn, chat_fn=None, model="")
    assert manifest["page_count"] == 1
    entry = manifest["pages"][0]
    assert set(entry) >= {"file", "id", "order", "slug", "source_refs", "summary", "title"}
    assert (wiki_dir / "orders.md").exists()
    saved = json.loads((wiki_dir / "manifest.json").read_text())
    assert saved["pages"][0]["slug"] == "orders"
