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


def _pkg_symbols(conn, pkg: str, n: int) -> None:
    """Insert `n` throwaway function symbols under `pkg` — enough to clear MIN_SYMBOLS."""
    for i in range(n):
        sid = f"{pkg}::fn{i}"
        conn.execute("INSERT OR IGNORE INTO files(path,language,sha256,size,n_symbols) "
                     "VALUES(?,?,?,?,1)", (f"{pkg}/f{i}.py", "python", sid, 1))
        conn.execute(
            "INSERT INTO symbols(id,file_path,kind,name,qualname,package,start_line,end_line,"
            "signature,content_hash,rollup_hash) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (sid, f"{pkg}/f{i}.py", "function", f"fn{i}", f"fn{i}", pkg, 1, 5, f"fn{i}", sid, sid))
    conn.commit()


def test_propose_does_not_duplicate_a_root_level_bucket(tmp_path):
    """Regression: a package whose modules sit directly at a repo root (package == "app", no
    subdirectory) used to get BOTH the overview page's `include` (which already lists "app" as
    a root) AND its own second page also `include: [app]` — an exact duplicate. Found by running
    `codewiki init` against codewiki's own repo, where root-level modules (build.py, config.py,
    ...) collided with the "codewiki" root itself."""
    conn = db.connect(tmp_path / "g.db")
    _pkg_symbols(conn, "app", 10)          # root-level modules — same package as the root itself
    _pkg_symbols(conn, "app/sub", 10)      # a real subpackage

    pages = scaffold.propose(conn)
    includes = [p["include"] for p in pages]
    assert includes.count(["app"]) == 1, f"'app' must appear as an include list exactly once: {pages}"
    assert ["app/sub"] in includes


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
