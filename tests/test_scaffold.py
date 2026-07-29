"""Tests for `codewiki init`'s pages.yaml scaffolding (assembly/scaffold.py)."""

import yaml

from codewiki.assembly import scaffold
from codewiki.assembly.pages import load_pages
from codewiki.assembly.render import _packages_for
from codewiki.store import db


def test_package_counts_excludes_tests(seeded_conn_with_tests):
    counts = scaffold.package_counts(seeded_conn_with_tests)
    assert "repo/pkg" in counts
    assert not any("test" in p for p in counts), "test packages get their own page, not a group"


def test_choose_groups_prefers_shallowest_split():
    counts = {"src/api": 20, "src/db": 20, "web/ui": 20}
    groups = dict(scaffold.choose_groups(counts))
    assert set(groups) == {"src", "web"}, "depth 1 already splits the repo"

    counts = {"src/api": 20, "src/db": 20}
    groups = dict(scaffold.choose_groups(counts))
    assert set(groups) == {"src/api", "src/db"}, "depth 1 is one group, so go deeper"


def test_choose_groups_falls_back_for_small_repos():
    """No group clears MIN_SYMBOLS — still propose something rather than nothing."""
    assert scaffold.choose_groups({"tiny/pkg": 2}) == [("tiny", 2)]


def test_propose_covers_real_packages(seeded_conn_with_tests):
    conn = seeded_conn_with_tests
    pages = scaffold.propose(conn)
    assert pages, "a seeded graph must yield a taxonomy"

    slugs = [p["slug"] for p in pages]
    assert "overview" in slugs
    assert "testing" in slugs

    testing = next(p for p in pages if p["slug"] == "testing")
    assert testing["keep_tests"] is True

    # every proposed page must actually match packages — that's the whole point
    for page in pages:
        assert _packages_for(conn, page["include"]), f"{page['slug']} matched nothing"


def test_propose_attaches_domain_kinds(seeded_conn):
    """The seeded graph has one `route` node; it should land on the page that contains it."""
    pages = scaffold.propose(seeded_conn)
    kinds = {k for p in pages for k in p.get("domain", [])}
    assert "route" in kinds or all(p["slug"] == "overview" for p in pages)


def test_propose_empty_graph_yields_nothing(tmp_path):
    conn = db.connect(tmp_path / "empty.db")
    assert scaffold.propose(conn) == []


def test_render_yaml_round_trips_through_the_loader(seeded_conn_with_tests, tmp_path):
    pages = scaffold.propose(seeded_conn_with_tests)
    text = scaffold.render_yaml(pages)

    parsed = yaml.safe_load(text)
    assert [p["slug"] for p in parsed["pages"]] == [p["slug"] for p in pages]

    dest = tmp_path / "pages.yaml"
    dest.write_text(text, encoding="utf-8")
    specs = load_pages(dest)
    assert [s.slug for s in specs] == [p["slug"] for p in pages]
    assert all(s.include for s in specs)
