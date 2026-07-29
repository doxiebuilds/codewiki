"""Section fill + page assembly: check_section semantics, retry/fallback flow, diagram-token
resolution, separators, and the deterministic section fallback."""

from codewiki.assembly.pages import PageSpec
from codewiki.writer import bundle as B
from codewiki.writer import sections as SEC
from codewiki.writer import skeleton as SK
from codewiki.writer import validate as V

from conftest import SEED_REL

SPEC = PageSpec(slug="orders", title="Orders", order=1,
                include=["repo/pkg"], domain=["route"])


def _fixture(conn):
    b = B.build_bundle(conn, SPEC, ["repo/pkg"])
    skel = SK.fallback_skeleton(SPEC, b)
    return b, skel


def _long(text: str) -> str:
    return " ".join(f"{text} sentence {i} explains the mechanism in enough depth."
                    for i in range(12))


# ------------------------------------------------------------------ check_section
def test_check_section_happy(seeded_conn):
    b, skel = _fixture(seeded_conn)
    sec = skel.sections[0]
    sec.heading = "Order Flow"
    allowed = {f"{SEED_REL}:1-9", f"{SEED_REL}:3-5"}
    md = (f"## Order Flow\n\n{_long('flow')}\n\n**Sources:**\n- {SEED_REL}:3-5\n- {SEED_REL}:1-9\n")
    report = V.check_section(seeded_conn, md, sec, allowed)
    assert report.errors == []
    assert report.n_sources == 2
    assert report.markdown.startswith("## Order Flow")
    assert "**Sources:**" in report.markdown              # re-rendered bare (linkified later)


def test_check_section_blocking_cases(seeded_conn):
    b, skel = _fixture(seeded_conn)
    sec = skel.sections[0]
    sec.heading = "Order Flow"
    allowed = {f"{SEED_REL}:1-9"}
    # wrong heading + thin block; missing Sources is REPAIRED (synthesized), not blocking
    r = V.check_section(seeded_conn, "## Wrong\n\nshort", sec, allowed)
    joined = " ".join(r.errors)
    assert "start with exactly" in joined and "too thin" in joined
    assert any("synthesized" in w for w in r.warnings)
    assert "**Sources:**" in r.markdown                       # synthesized from evidence
    # inline paths beyond the threshold still block
    spam = " ".join(f"see apps/foo/bar{i}.py:1" for i in range(5))
    md = f"## Order Flow\n\n{_long('x')} {spam}\n\n**Sources:**\n- {SEED_REL}:1-9\n- {SEED_REL}:2-8\n"
    r2 = V.check_section(seeded_conn, md, sec, allowed)
    assert any("raw file paths in prose" in e for e in r2.errors)


def test_table_rows_exempt_from_inline_path_rule(seeded_conn):
    b, skel = _fixture(seeded_conn)
    sec = skel.sections[0]
    sec.heading = "Reference"
    allowed = {f"{SEED_REL}:1-9"}
    table = ("| Env var | First seen |\n|---|---|\n"
             + "\n".join(f"| `X{i}` | `repo/pkg/orders.py:{i+1}` |"
                         for i in range(8)))
    md = f"## Reference\n\n{_long('reference')}\n\n{table}\n\n**Sources:**\n- {SEED_REL}:1-9\n- {SEED_REL}:2-8\n"
    r = V.check_section(seeded_conn, md, sec, allowed)
    assert not any("raw file paths" in e for e in r.errors)
    assert "repo/pkg/orders.py:3" in r.markdown   # cells untouched


def test_section_fallback_is_valid_markdown(seeded_conn):
    b, skel = _fixture(seeded_conn)
    sec = next(s for s in skel.sections if s.evidence)
    md = SEC.section_fallback(sec, b)
    assert md.startswith(f"## {sec.heading}")
    assert "- **" in md                                    # bold lead-in bullets


# ------------------------------------------------------------------ fill_section
def test_fill_section_retry_flow(seeded_conn):
    b, skel = _fixture(seeded_conn)
    sec = next(s for s in skel.sections
               if s.evidence and len(B.allowed_cites(b, s.evidence)) >= 2)
    prefix = SEC.build_shared_prefix(SPEC, skel, b)
    calls = {"n": 0}
    locs = sorted(B.allowed_cites(b, sec.evidence))[:2]

    def stub(prompt, *, model=None, system="", max_tokens=None, timeout=None, **kw):
        calls["n"] += 1
        usage = {"prompt_tokens": 1, "completion_tokens": 1, "finish_reason": "stop"}
        if calls["n"] == 1:
            return "junk", usage
        entries = "\n".join(f"- {t}" for t in locs) or f"- {SEED_REL}:1-9"
        return f"## {sec.heading}\n\n{_long(sec.heading)}\n\n**Sources:**\n{entries}\n", usage

    md, usage, info = SEC.fill_section(seeded_conn, sec, skel, prefix, b,
                                       chat_fn=stub, model="m")
    assert calls["n"] == 2 and info["retried"] and not info["fallback"]
    assert md.startswith(f"## {sec.heading}")


# ------------------------------------------------------------------ assemble_page
def test_assemble_page_diagram_token_paths(seeded_conn):
    b, skel = _fixture(seeded_conn)
    s1, s2 = skel.sections[0], skel.sections[1]
    block = "```mermaid\nflowchart TD\n  A[\"a\"] -- \"x()\" --> B[\"b\"]\n```"
    md1 = f"## {s1.heading}\n\nintro para.\n\n[[DIAGRAM:{s1.id}]]\n\nmore."
    md2 = f"## {s2.heading}\n\nprose only.\n\n[[DIAGRAM:ZZ]]\n"        # stray token
    page, errors, warnings = SEC.assemble_page(
        seeded_conn, "Orders", [(s1, md1), (s2, md2)], {s1.id: block, s2.id: block})
    assert page.startswith("# Orders\n")
    assert page.count("```mermaid") == 2
    assert "[[DIAGRAM" not in page                        # token replaced; stray line dropped
    assert "\n---\n" in page
    # s2's planned block was inserted after its first paragraph despite the missing token
    assert page.index("prose only.") < page.index("```mermaid", page.index("prose only."))


def test_assemble_page_flags_thin_and_duplicates(seeded_conn):
    b, skel = _fixture(seeded_conn)
    s1, s2 = skel.sections[0], skel.sections[1]
    same = f"## H\n\n{_long('identical twin prose')}"
    page, errors, warnings = SEC.assemble_page(
        seeded_conn, "Orders", [(s1, same), (s2, same.replace('## H', '## H2'))], {})
    assert any("Sources entries page-wide" in e for e in errors)
    assert any("near-duplicates" in w for w in warnings)
