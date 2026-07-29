"""Prefix-cache economics guard: every section call of a page must share a byte-identical
prompt prefix with all variability at the tail, and the prefix must contain nothing dynamic."""

import re

from codewiki.assembly.pages import PageSpec
from codewiki.writer import bundle as B
from codewiki.writer import sections as SEC
from codewiki.writer import skeleton as SK

SPEC = PageSpec(slug="orders", title="Orders", order=1,
                include=["repo/pkg"], domain=["route"])

_ISO_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}")


def test_shared_prefix_is_byte_identical_across_section_prompts(seeded_conn):
    b = B.build_bundle(seeded_conn, SPEC, ["repo/pkg"])
    skel = SK.fallback_skeleton(SPEC, b)
    prefix = SEC.build_shared_prefix(SPEC, skel, b)
    prefix_again = SEC.build_shared_prefix(SPEC, skel, b)
    assert prefix == prefix_again                                # deterministic rebuild

    prompts = [prefix + SEC.section_tail(sec, skel, b) for sec in skel.sections]
    assert len(prompts) >= 4
    for p in prompts:
        assert p.startswith(prefix)                              # byte-identical shared head
    # variability strictly at the tail: tails must differ, prefix must not
    tails = [p[len(prefix):] for p in prompts]
    assert len(set(tails)) == len(tails)


def test_prefix_contains_nothing_dynamic(seeded_conn):
    b = B.build_bundle(seeded_conn, SPEC, ["repo/pkg"])
    skel = SK.fallback_skeleton(SPEC, b)
    prefix = SEC.build_shared_prefix(SPEC, skel, b)
    assert not _ISO_DATE_RE.search(prefix)                       # no timestamps/dates
    assert "RECENT CHANGES" not in prefix                        # git evidence stays in tails


def test_retry_prompt_preserves_the_prefix(seeded_conn):
    """A section retry appends to the SAME prefix (cache still hits)."""
    b = B.build_bundle(seeded_conn, SPEC, ["repo/pkg"])
    skel = SK.fallback_skeleton(SPEC, b)
    prefix = SEC.build_shared_prefix(SPEC, skel, b)
    sec = skel.sections[0]
    seen: list[str] = []

    def stub(prompt, *, model=None, system="", max_tokens=None, timeout=None, **kw):
        seen.append(prompt)
        return "junk", {"prompt_tokens": 1, "completion_tokens": 1, "finish_reason": "stop"}

    SEC.fill_section(seeded_conn, sec, skel, prefix, b, chat_fn=stub, model="m")
    assert len(seen) == 2                                        # initial + retry
    assert all(p.startswith(prefix) for p in seen)
