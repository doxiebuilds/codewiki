"""Built-in domain extractors, run against a synthetic mini repo (see indexer/domain/builtin.py).

`extract_services` (a repo-specific service-tier extractor) isn't part of the built-in set —
see examples/plugins/services_tiers.py for how to write and register one like it.
"""

import json

import pytest

from codewiki import paths as P
from codewiki.indexer.domain import builtin as D
from codewiki.store import db


@pytest.fixture
def mini_repo(tmp_path, monkeypatch):
    (tmp_path / "app").mkdir()
    (tmp_path / "schema").mkdir()
    (tmp_path / "frontend").mkdir()

    (tmp_path / "app" / "routes.py").write_text(
        'from fastapi import APIRouter\n'
        'router = APIRouter()\n\n'
        '@router.get("/api/widgets")\n'
        'def list_widgets():\n'
        '    return []\n\n'
        '@router.post("/api/gadgets")\n'
        'def create_gadget():\n'
        '    return {}\n'
    )
    (tmp_path / "app" / "config.py").write_text(
        'import os\n'
        'FLAG = os.environ.get("MY_APP_FLAG", "default")\n'
        'OTHER = os.environ["MY_APP_OTHER"]\n'
    )
    (tmp_path / "schema" / "init.sql").write_text(
        "CREATE TABLE IF NOT EXISTS widgets (id INTEGER PRIMARY KEY);\n"
        "CREATE TABLE orders (id INTEGER);\n"
    )
    (tmp_path / "frontend" / "api.ts").write_text(
        'fetch("/api/widgets").then(r => r.json());\n'
    )

    monkeypatch.setattr(D, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(P, "REPO_ROOT", tmp_path)
    return tmp_path


def test_routes_extracted(mini_repo):
    routes = D.extract_routes(None)
    methods = {(json.loads(r["detail"])["method"], r["name"]) for r in routes}
    assert ("GET", "/api/widgets") in methods
    assert ("POST", "/api/gadgets") in methods
    assert all(r["line"] for r in routes)


def test_db_tables_extracted(mini_repo):
    tables = D.extract_db_tables(None)
    names = {t["name"] for t in tables}
    assert {"widgets", "orders"} <= names
    assert all(t["file_path"].endswith(".sql") for t in tables)


def test_env_flags_extracted(mini_repo):
    flags = D.extract_env_flags(None)
    names = {f["name"] for f in flags}
    assert {"MY_APP_FLAG", "MY_APP_OTHER"} <= names


def test_api_calls_matched_to_routes(mini_repo):
    conn = db.connect(mini_repo / "g.db")
    db.replace_domain_nodes(conn, "route", D.extract_routes(None))
    conn.commit()
    calls = D.extract_api_calls(conn)
    hit = next(c for c in calls if c["name"] == "/api/widgets")
    detail = json.loads(hit["detail"])
    assert detail["route"] == "GET /api/widgets"


def test_route_matching_tolerates_templates_and_api_prefix():
    assert D._route_matches("/api/candles/${symbol}", "/candles/{symbol}")
    assert D._route_matches("/api/quotes/AAPL", "/api/quotes/{symbol}")
    assert not D._route_matches("/api/candles/x/y", "/candles/{symbol}")
