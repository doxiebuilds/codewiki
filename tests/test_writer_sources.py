"""Sources blocks: split/repair/render — exact GitHub link format, rootless repair, clamping,
out-of-slice drops, inline-path hygiene, final page linkification."""

from codewiki.writer import sources as SRC

from conftest import SEED_REL

SECTION = """## Order Flow

Orders route through validation first.

**Sources:**
- repo/pkg/orders.py:3-5
- pkg/orders.py:7-9
- repo/pkg/orders.py:3-999
- repo/no/such.py:1
- not a source line
"""


def test_split_sources():
    body, entries = SRC.split_sources(SECTION)
    assert "**Sources:**" not in body
    assert entries == ["repo/pkg/orders.py:3-5", "pkg/orders.py:7-9",
                       "repo/pkg/orders.py:3-999",
                       "repo/no/such.py:1"]
    assert SRC.split_sources("## X\n\nno sources")[1] is None


def test_repair_entries_prefix_clamp_drop(seeded_conn):
    _, entries = SRC.split_sources(SECTION)
    kept, notes = SRC.repair_entries(seeded_conn, entries, None)
    paths = [(p, a, b) for p, a, b in kept]
    assert (SEED_REL, 3, 5) in paths                     # valid kept
    assert (SEED_REL, 7, 9) in paths                     # rootless → prefixed
    max_line = SRC._file_max_line(seeded_conn, SEED_REL)
    assert (SEED_REL, 3, max_line) in paths              # 999 clamped
    assert not any("no/such.py" in p for p, _, _ in kept)
    assert any("unknown file" in n for n in notes)


def test_repair_entries_respects_allowed_slice(seeded_conn):
    # allowed window is the evidence range ±SLACK (5) — use ranges far enough apart
    allowed = {f"{SEED_REL}:1-2"}
    kept, notes = SRC.repair_entries(
        seeded_conn, [f"{SEED_REL}:1-2", f"{SEED_REL}:8-9"], allowed)
    assert [(p, a, b) for p, a, b in kept] == [(SEED_REL, 1, 2)]   # 8-9 exceeds 2+5
    assert any("outside this section's evidence" in n for n in notes)
    # ±SLACK tolerance: 1-7 fits inside [1-5 .. 2+5]
    kept2, _ = SRC.repair_entries(seeded_conn, [f"{SEED_REL}:1-7"], allowed)
    assert kept2


def test_render_sources_github_format():
    md = SRC.render_sources([(SEED_REL, 3, 5), (SEED_REL, 7, None)])
    assert ("- [orders.py:3-5](https://github.com/example/repo/blob/main/"
            f"{SEED_REL}#L3-L5)") in md
    assert f"- [orders.py:7](https://github.com/example/repo/blob/main/{SEED_REL}#L7)" in md


def test_render_sources_basename_collision():
    md = SRC.render_sources([("repo/a/x.py", 1, 2),
                             ("repo/b/x.py", 3, 4)])
    assert "[a/x.py:1-2]" in md and "[b/x.py:3-4]" in md


def test_strip_inline_paths_leaves_fences_alone():
    prose = ("See apps/api/runtime/wiki.py:42 for details.\n"
             "```\napps/api/runtime/wiki.py stays verbatim in code\n```\n"
             "Also `packages/storage/db.py`.")
    out, n = SRC.strip_inline_paths(prose)
    assert n == 2
    assert "`wiki.py`" in out and "`db.py`" in out
    assert "apps/api/runtime/wiki.py stays verbatim in code" in out


def test_linkify_page_converts_bare_blocks(seeded_conn):
    page = f"# T\n\n## S\n\nprose\n\n**Sources:**\n- {SEED_REL}:3-5\n\n---\n\nmore"
    out = SRC.linkify_page(seeded_conn, page)
    assert "github.com/example/repo/blob/main/" in out
    assert f"- {SEED_REL}:3-5" not in out
    # idempotent: linkified blocks are left alone on a second pass
    assert SRC.linkify_page(seeded_conn, out) == out
