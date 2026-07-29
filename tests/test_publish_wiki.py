"""Tests for the docs/wiki -> GitHub wiki mirror (scripts/publish_wiki.py).

The push path talks to a real remote, so it isn't covered here; everything that decides what the
wiki *contains* — page naming and link rewriting — is pure and is covered.
"""

import json

import pytest

from scripts.publish_wiki import (
    build,
    build_name_map,
    render_footer,
    render_sidebar,
    repo_web_url,
    rewrite_links,
    wiki_name,
)

PAGES = [
    {"file": "quickstart.md", "order": 0, "slug": "quickstart", "title": "Quickstart"},
    {"file": "overview.md", "order": 1, "slug": "overview", "title": "Overview"},
    {"file": "store.md", "order": 2, "slug": "store", "title": "Store"},
]


@pytest.fixture
def name_map():
    return build_name_map(PAGES)


def test_lowest_ordered_page_becomes_home(name_map):
    """GitHub serves `Home` as the wiki landing page — the taxonomy's first page has to take it."""
    assert name_map["quickstart"] == "Home"
    assert name_map["overview"] == "Overview"


def test_wiki_name_dashes_spaces():
    assert wiki_name("Getting Started") == "Getting-Started", "GitHub maps spaces to dashes in URLs"


def test_rewrite_links_targets_page_names(name_map):
    assert rewrite_links("see [Store](store.md)", name_map) == "see [Store](Store)"
    assert rewrite_links("[Quickstart](quickstart.md)", name_map) == "[Quickstart](Home)"


def test_rewrite_links_preserves_anchors(name_map):
    assert rewrite_links("[x](overview.md#the-graph)", name_map) == "[x](Overview#the-graph)"


def test_rewrite_links_leaves_source_citations_alone(name_map):
    """Generated source_refs are absolute blob URLs — they stay valid from inside the wiki."""
    cite = "[db.py](https://github.com/o/r/blob/main/codewiki/store/db.py#L1-L20)"
    assert rewrite_links(cite, name_map) == cite


def test_rewrite_links_falls_back_for_unknown_pages(name_map):
    """An unmanifested target still can't keep `.md` — that 404s in a wiki."""
    assert rewrite_links("[gone](./legacy.md)", name_map) == "[gone](legacy)"


def test_sidebar_follows_taxonomy_order(name_map):
    lines = [line for line in render_sidebar(PAGES, name_map).splitlines() if line.startswith("-")]
    assert lines == ["- [Quickstart](Home)", "- [Overview](Overview)", "- [Store](Store)"]


def test_footer_pins_the_source_commit():
    footer = render_footer({"model": "m", "generated_at": "2026-07-29T00:00:00+00:00"},
                           "https://github.com/o/r", "abc1234")
    assert "https://github.com/o/r/tree/abc1234" in footer
    assert "2026-07-29" in footer


@pytest.mark.parametrize("remote", ["git@github.com:o/r.git", "https://github.com/o/r.git",
                                    "https://github.com/o/r"])
def test_repo_web_url_normalizes_remotes(remote):
    assert repo_web_url(remote) == "https://github.com/o/r"


def _seed_wiki(wiki_dir, pages=PAGES):
    wiki_dir.mkdir(parents=True)
    (wiki_dir / "manifest.json").write_text(json.dumps({"model": "m", "pages": pages}))
    for page in pages:
        (wiki_dir / page["file"]).write_text(f"# {page['title']}\n\n[Store](store.md)\n")


def test_build_writes_a_wiki_shaped_tree(tmp_path):
    _seed_wiki(tmp_path / "wiki")
    out = tmp_path / "out"

    written = build(tmp_path / "wiki", out, "https://github.com/o/r", "abc1234")

    assert written == ["Home", "Overview", "Store"]
    assert {p.name for p in out.iterdir()} == {
        "Home.md", "Overview.md", "Store.md", "_Sidebar.md", "_Footer.md", "manifest.json"}
    assert "[Store](Store)" in (out / "Home.md").read_text()


def test_build_replaces_stale_output(tmp_path):
    """Output is a mirror: a page dropped from the taxonomy must not survive the next build."""
    _seed_wiki(tmp_path / "wiki")
    out = tmp_path / "out"
    out.mkdir()
    (out / "Removed.md").write_text("stale")

    build(tmp_path / "wiki", out, "https://github.com/o/r", "abc1234")

    assert not (out / "Removed.md").exists()


def test_build_rejects_a_manifest_that_lies(tmp_path):
    _seed_wiki(tmp_path / "wiki")
    (tmp_path / "wiki" / "store.md").unlink()

    with pytest.raises(SystemExit, match="store.md"):
        build(tmp_path / "wiki", tmp_path / "out", "https://github.com/o/r", "abc1234")
