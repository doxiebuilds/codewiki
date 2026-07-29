import hashlib
import json
import sys
from pathlib import Path

import pytest

# Put the package parent dir on sys.path so `import codewiki` resolves (the package isn't
# pip-installed in this checkout).
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from codewiki import config as C


@pytest.fixture(autouse=True)
def _fixed_config(monkeypatch, tmp_path):
    """Deterministic config for every test: fixed repo root, source-subdir prefix, and GitHub
    base — independent of wherever this checkout actually lives or its real git remote."""
    monkeypatch.setenv("CODEWIKI_REPO_ROOT", str(tmp_path))
    monkeypatch.setenv("CODEWIKI_SOURCE_SUBDIR", "repo")
    monkeypatch.setenv("CODEWIKI_GITHUB_BASE", "https://github.com/example/repo/blob/main/")
    C.reset_cache()
    yield
    C.reset_cache()


SEED_SRC = b'''"""orders module."""

def submit_order(o):
    """Submit an order."""
    return validate(o)

class OrderBook:
    def add(self, level):
        return 1
'''

SEED_REL = "repo/pkg/orders.py"

# a sibling test file — exercises test-path de-prioritization in bundles
SEED_TEST_SRC = b'''"""tests for orders."""

def test_submit_order_roundtrip():
    assert submit_order({"qty": 1})

def test_orderbook_add_levels():
    assert OrderBook().add(1) == 1

def test_orderbook_rejects_negative():
    assert OrderBook().add(-1) == 1

def test_submit_order_empty():
    assert not submit_order(None)
'''

SEED_TEST_REL = "repo/pkg/tests/test_orders.py"


@pytest.fixture
def seeded_conn(tmp_path):
    """Synthetic single-file graph + stub summaries + one route node (shared by tests)."""
    return _seed(tmp_path, with_tests=False)


@pytest.fixture
def seeded_conn_with_tests(tmp_path):
    """Same graph plus a sibling tests/ file (for de-prioritization tests)."""
    return _seed(tmp_path, with_tests=True)


def _seed(tmp_path, *, with_tests: bool):
    from codewiki.generator import summarize as S
    from codewiki.indexer import graph
    from codewiki.indexer.discovery import FileMeta
    from codewiki.store import db

    conn = db.connect(tmp_path / "g.db")
    p = tmp_path / "orders.py"
    p.write_bytes(SEED_SRC)
    fm = FileMeta(path=SEED_REL, abs=p, language="python",
                  sha256=hashlib.sha256(SEED_SRC).hexdigest(), size=len(SEED_SRC))
    graph.index_file(conn, fm)
    if with_tests:
        pt = tmp_path / "test_orders.py"
        pt.write_bytes(SEED_TEST_SRC)
        fmt = FileMeta(path=SEED_TEST_REL, abs=pt, language="python",
                       sha256=hashlib.sha256(SEED_TEST_SRC).hexdigest(), size=len(SEED_TEST_SRC))
        graph.index_file(conn, fmt)
    db.replace_domain_nodes(conn, "route", [{
        "id": "route::GET /api/orders", "kind": "route", "name": "/api/orders",
        "detail": json.dumps({"method": "GET", "func": "list_orders"}),
        "file_path": SEED_REL, "line": 3}])
    conn.commit()
    stub = lambda prompt, *, model=None, **kw: ('{"summary": "does a thing"}',
                                                {"prompt_tokens": 1, "completion_tokens": 1})
    S.summarize_all(conn, chat_fn=stub, only_stale=True)
    return conn
