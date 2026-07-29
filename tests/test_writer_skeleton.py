"""Skeleton planning: parse robustness, deterministic fix-ups, blocking errors, fallback."""

import json

from codewiki.assembly.pages import PageSpec
from codewiki.writer import bundle as B
from codewiki.writer import skeleton as SK

SPEC = PageSpec(slug="orders", title="Orders", order=1,
                include=["repo/pkg"], domain=["route"])


def _bundle(conn):
    return B.build_bundle(conn, SPEC, ["repo/pkg"])


def _plan(b, sections):
    return {"title": "Orders", "sections": sections}


def _sec(heading, evidence, **kw):
    return {"heading": heading, "evidence": evidence, **kw}


def test_parse_strips_think_and_fences():
    raw = '<think>hmm</think>```json\n{"title": "T", "sections": []}\n```'
    assert SK.parse_skeleton(raw) == {"title": "T", "sections": []}
    assert SK.parse_skeleton("no json here") is None


def test_valid_plan_normalizes_ids_and_pins_architecture(seeded_conn):
    b = _bundle(seeded_conn)
    eids = [it.eid for it in b.evidence if it.cites][:4]
    data = _plan(b, [
        _sec("Purpose and Scope", eids),
        _sec("Architecture", eids),
        _sec("Order Flow", eids, diagram={"type": "architecture"}),   # misplaced arch slot
        _sec("Where to Start & Watch-Outs", eids),
    ])
    skel, errors = SK.validate_skeleton(data, b, SPEC)
    assert errors == [] and skel is not None
    assert [s.id for s in skel.sections] == [f"S{i+1}" for i in range(len(skel.sections))]
    if b.det_diagram:
        assert skel.sections[1].diagram == {"type": "architecture"}
    assert all((s.diagram or {}).get("type") != "architecture"
               for s in skel.sections if s is not skel.sections[1])


def test_missing_watchouts_is_appended(seeded_conn):
    b = _bundle(seeded_conn)
    eids = [it.eid for it in b.evidence if it.cites][:4]
    data = _plan(b, [_sec("Purpose and Scope", eids), _sec("Architecture", eids),
                     _sec("Flow", eids), _sec("Persistence", eids)])
    skel, errors = SK.validate_skeleton(data, b, SPEC)
    assert errors == []
    assert SK._is_watchouts(skel.sections[-1].heading)


def test_unknown_eids_block(seeded_conn):
    b = _bundle(seeded_conn)
    data = _plan(b, [_sec("A", ["E999"]), _sec("B", ["E1"]),
                     _sec("C", ["E1"]), _sec("D", ["E1"])])
    skel, errors = SK.validate_skeleton(data, b, SPEC)
    assert skel is None and any("E999" in e for e in errors)


def test_too_many_sections_are_merged(seeded_conn):
    b = _bundle(seeded_conn)
    eids = [it.eid for it in b.evidence if it.cites][:4] or [b.evidence[0].eid, b.evidence[0].eid]
    data = _plan(b, [_sec(f"Section {i}", eids) for i in range(12)])
    skel, errors = SK.validate_skeleton(data, b, SPEC)
    assert errors == []
    assert len(skel.sections) <= SK.MAX_SECTIONS + 1     # +1: appended watch-outs tail


def test_duplicate_headings_merge_evidence(seeded_conn):
    b = _bundle(seeded_conn)
    all_eids = [it.eid for it in b.evidence]
    data = _plan(b, [
        _sec("Purpose and Scope", all_eids[:2]),
        _sec("Flow", all_eids[:1]) | {"evidence": all_eids[:2]},
        _sec("Flow", all_eids[2:4]),
        _sec("Where to Start & Watch-Outs", all_eids[:2]),
    ])
    skel, errors = SK.validate_skeleton(data, b, SPEC)
    assert errors == []
    flows = [s for s in skel.sections if s.heading == "Flow"]
    assert len(flows) == 1
    assert set(all_eids[2:4]) <= set(flows[0].evidence)


def test_fallback_skeleton_is_deterministic_and_complete(seeded_conn):
    b = _bundle(seeded_conn)
    s1 = SK.fallback_skeleton(SPEC, b)
    s2 = SK.fallback_skeleton(SPEC, b)
    assert json.dumps(s1.to_dict()) == json.dumps(s2.to_dict())
    assert s1.source == "fallback"
    assert s1.sections[0].heading == "Purpose and Scope"
    assert SK._is_watchouts(s1.sections[-1].heading)
    valid = {it.eid for it in b.evidence}
    assert all(e in valid for s in s1.sections for e in s.evidence)


def test_plan_page_retry_then_fallback(seeded_conn):
    b = _bundle(seeded_conn)
    calls = {"n": 0}

    def stub(prompt, **kw):
        calls["n"] += 1
        return "garbage", {"prompt_tokens": 1, "completion_tokens": 1}

    skel, usage = SK.plan_page(seeded_conn, SPEC, b, chat_fn=stub, model="m")
    assert calls["n"] == 2                       # first call + one retry
    assert skel.source == "fallback"
