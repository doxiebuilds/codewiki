"""pw2 writer guarantees, proven with a scripted mock LLM:
first run = 1 planner + N section calls and records the build; rerun = 0 calls; a one-function
edit regenerates only its page; a section that fails validation retries once then falls back
WITHOUT sinking the page; >1/3 fallback sections ⇒ page written but NO hash stored."""

import hashlib
import json
import re

import pytest

from codewiki.assembly.pages import PageSpec
from codewiki.indexer import graph
from codewiki.indexer.discovery import FileMeta
from codewiki.store import db
from codewiki.writer import bundle as B
from codewiki.writer import prompts
from codewiki.writer import write as W

from conftest import SEED_REL, SEED_SRC

SPEC = PageSpec(slug="orders", title="Orders", order=1,
                include=["repo/pkg"], domain=["route"])

_HEADING_RE = re.compile(r"^HEADING: (.+)$", re.MULTILINE)
_LOC_RE = re.compile(r"^LOCATIONS: (.+)$", re.MULTILINE)


def _prose(heading: str) -> str:
    """Long, per-section-distinct prose (defeats the thin-page and near-duplicate checks)."""
    words = heading.lower().split()
    return " ".join(
        f"In the {words[0]} stage number {i}, the {' '.join(words)} path validates each order "
        f"before the {words[-1]} book mutates, keeping malformed {words[0]} submissions out "
        f"of shared state entirely."
        for i in range(10))


def _planner_json(conn, spec=SPEC):
    """A valid plan built from the REAL bundle's eids (deterministic across runs)."""
    packages = ["repo/pkg"]
    if conn.execute("SELECT 1 FROM symbols WHERE package='repo/pkg/tests'").fetchone():
        packages.append("repo/pkg/tests")
    b = B.build_bundle(conn, spec, packages)
    cited = [it.eid for it in b.evidence if it.cites]
    table = next((it for it in b.evidence if it.kind == "table"), None)
    plan = {"title": spec.title, "sections": [
        {"id": "S1", "heading": "Purpose and Scope", "brief": "What this does.",
         "evidence": cited[:4] or [b.evidence[0].eid]},
        {"id": "S2", "heading": "Architecture", "brief": "How it fits.",
         "diagram": {"type": "architecture"}, "evidence": cited[:4]},
        {"id": "S3", "heading": "Order Flow", "brief": "Runtime path.",
         "table": table.data["title"] if table else None, "evidence": cited[:4]},
        {"id": "S4", "heading": "Where to Start & Watch-Outs", "brief": "Guidance.",
         "evidence": cited[:4]},
    ]}
    return json.dumps(plan)


def _good_section(prompt: str) -> str:
    heading = _HEADING_RE.search(prompt).group(1)
    locs: list[str] = []
    for m in _LOC_RE.finditer(prompt):
        locs += [t.strip() for t in m.group(1).split(",")]
    entries = "\n".join(f"- {t}" for t in dict.fromkeys(locs)) or "- unknown.py:1"
    return f"## {heading}\n\n{_prose(heading)}\n\n**Sources:**\n{entries}\n"


def make_llm(conn, *, section_fn=_good_section, planner=None, quickstart="A fine intro."):
    calls = {"planner": 0, "section": 0, "diagram": 0, "quickstart": 0, "total": 0}
    planner_text = planner if planner is not None else _planner_json(conn)

    def stub(prompt, *, model=None, system="", max_tokens=None, timeout=None, **kw):
        calls["total"] += 1
        usage = {"prompt_tokens": 100, "completion_tokens": 100, "finish_reason": "stop"}
        if system == prompts.PLANNER_SYSTEM:
            calls["planner"] += 1
            return planner_text, usage
        if system == prompts.SECTION_SYSTEM:
            calls["section"] += 1
            return section_fn(prompt), usage
        if system == prompts.DIAGRAM_SYSTEM:
            calls["diagram"] += 1
            return "", usage
        calls["quickstart"] += 1
        return quickstart, usage

    return stub, calls


@pytest.fixture(autouse=True)
def _no_network_budget(monkeypatch):
    monkeypatch.setenv("CODEWIKI_CTX", "32768")   # skip the LM Studio context probe


# ------------------------------------------------------------------ happy path + gating
def test_first_run_writes_and_records(seeded_conn, tmp_path):
    stub, calls = make_llm(seeded_conn)
    result, entry = W.write_page(seeded_conn, SPEC, chat_fn=stub, model="m",
                                 out_dir=tmp_path, git_head="abc")
    assert result.status == "written" and result.fresh
    assert calls["planner"] == 1 and calls["section"] == 4
    md = (tmp_path / "orders.md").read_text()
    assert md.startswith("# Orders\n")
    assert "\n---\n" in md                                   # section separators
    assert "github.com/example/repo/blob/main/" in md   # linkified Sources
    assert "repo/" not in md.replace(
        "github.com/example/repo/blob/main/repo/", "")
    build = db.get_page_build(seeded_conn, "orders")
    assert build and build["status"] == "written" and build["git_head"] == "abc"
    assert json.loads(build["skeleton_json"])["sections"]
    assert entry["written_at"] and entry["slug"] == "orders"


def test_rerun_is_zero_llm_calls(seeded_conn, tmp_path):
    stub, _ = make_llm(seeded_conn)
    W.write_page(seeded_conn, SPEC, chat_fn=stub, model="m", out_dir=tmp_path)
    stub2, calls2 = make_llm(seeded_conn)
    result, _ = W.write_page(seeded_conn, SPEC, chat_fn=stub2, model="m", out_dir=tmp_path)
    assert result.status == "skipped_fresh" and calls2["total"] == 0


def test_one_function_edit_regenerates_the_page(seeded_conn, tmp_path):
    stub, _ = make_llm(seeded_conn)
    W.write_page(seeded_conn, SPEC, chat_fn=stub, model="m", out_dir=tmp_path)

    edited = SEED_SRC.replace(b"return 1", b"return 2")
    p = tmp_path / "orders_v2.py"
    p.write_bytes(edited)
    graph.index_file(seeded_conn, FileMeta(path=SEED_REL, abs=p, language="python",
                                           sha256=hashlib.sha256(edited).hexdigest(),
                                           size=len(edited)))
    seeded_conn.commit()

    stub2, calls2 = make_llm(seeded_conn)
    result, _ = W.write_page(seeded_conn, SPEC, chat_fn=stub2, model="m", out_dir=tmp_path)
    assert result.status == "written" and calls2["planner"] == 1


def test_model_or_prompt_change_invalidates(seeded_conn, tmp_path, monkeypatch):
    stub, _ = make_llm(seeded_conn)
    W.write_page(seeded_conn, SPEC, chat_fn=stub, model="m", out_dir=tmp_path)
    stub2, calls2 = make_llm(seeded_conn)
    result, _ = W.write_page(seeded_conn, SPEC, chat_fn=stub2, model="OTHER", out_dir=tmp_path)
    assert result.status == "written" and calls2["total"] > 0

    monkeypatch.setattr(prompts, "WRITER_PROMPT_VERSION", "pw-test-bump")
    stub3, calls3 = make_llm(seeded_conn)
    result, _ = W.write_page(seeded_conn, SPEC, chat_fn=stub3, model="OTHER", out_dir=tmp_path)
    assert result.status == "written" and calls3["total"] > 0


# ------------------------------------------------------------------ section-level degradation
def test_one_bad_section_retries_then_falls_back_page_stays_fresh(seeded_conn, tmp_path):
    bad_headings = {"Purpose and Scope"}

    def section_fn(prompt):
        heading = _HEADING_RE.search(prompt).group(1)
        if heading in bad_headings:
            return "junk without heading or sources"
        return _good_section(prompt)

    stub, calls = make_llm(seeded_conn, section_fn=section_fn)
    result, _ = W.write_page(seeded_conn, SPEC, chat_fn=stub, model="m", out_dir=tmp_path)
    # 3 good sections × 1 call + 1 bad section × 2 calls (retry) = 5
    assert calls["section"] == 5
    assert result.status == "written" and result.fresh          # 1/4 fallback ≤ 1/3
    assert db.get_page_build(seeded_conn, "orders") is not None
    md = (tmp_path / "orders.md").read_text()
    assert "## Purpose and Scope" in md                          # fallback section present


def test_majority_fallback_writes_file_but_stores_no_hash(seeded_conn, tmp_path):
    stub, calls = make_llm(seeded_conn, section_fn=lambda p: "junk")
    result, _ = W.write_page(seeded_conn, SPEC, chat_fn=stub, model="m", out_dir=tmp_path)
    assert result.status == "written" and not result.fresh
    assert (tmp_path / "orders.md").exists()
    assert db.get_page_build(seeded_conn, "orders") is None      # retried next run

    stub2, calls2 = make_llm(seeded_conn)                        # healthy model next run
    result2, _ = W.write_page(seeded_conn, SPEC, chat_fn=stub2, model="m", out_dir=tmp_path)
    assert result2.status == "written" and result2.fresh         # self-healed


def test_truncated_section_skips_retry(seeded_conn, tmp_path):
    seen = {"n": 0}

    def stub(prompt, *, model=None, system="", max_tokens=None, timeout=None, **kw):
        usage = {"prompt_tokens": 1, "completion_tokens": 1, "finish_reason": "stop"}
        if system == prompts.PLANNER_SYSTEM:
            return _planner_json(seeded_conn), usage
        if system == prompts.SECTION_SYSTEM:
            seen["n"] += 1
            heading = _HEADING_RE.search(prompt).group(1)
            if heading == "Order Flow":
                return "## Order Flow\n\ntruncat", {**usage, "finish_reason": "length"}
            return _good_section(prompt), usage
        return "intro", usage

    result, _ = W.write_page(seeded_conn, SPEC, chat_fn=stub, model="m", out_dir=tmp_path)
    assert seen["n"] == 4                                        # NO retry for the truncated one
    assert result.status == "written" and result.fresh           # 1/4 fallback


def test_planner_transport_failure_falls_back_to_jinja(seeded_conn, tmp_path):
    def stub(prompt, **kw):
        raise OSError("connection refused")

    result, entry = W.write_page(seeded_conn, SPEC, chat_fn=stub, model="m", out_dir=tmp_path)
    assert result.status == "fallback_jinja" and not result.fresh
    assert (tmp_path / "orders.md").exists()
    assert db.get_page_build(seeded_conn, "orders") is None


def test_bad_planner_json_uses_fallback_skeleton(seeded_conn, tmp_path):
    stub, calls = make_llm(seeded_conn, planner="this is not json at all")
    result, _ = W.write_page(seeded_conn, SPEC, chat_fn=stub, model="m", out_dir=tmp_path)
    assert calls["planner"] == 2                                 # one retry, then fallback skeleton
    assert result.status == "written"
    build = db.get_page_build(seeded_conn, "orders")
    if build:                                                    # fresh iff sections mostly held
        assert json.loads(build["validator_json"])["planner"] == "fallback"


# ------------------------------------------------------------------ assemble_writer
def test_assemble_writer_manifest_and_quickstart(seeded_conn, tmp_path, monkeypatch):
    monkeypatch.setattr(W, "load_pages", lambda: [SPEC])
    stub, _ = make_llm(seeded_conn)
    ticks = []
    manifest = W.assemble_writer(seeded_conn, chat_fn=stub, model="m",
                                 out_dir=tmp_path, verbose=False,
                                 on_progress=lambda d, t: ticks.append((d, t)))
    assert manifest["page_count"] == 2
    slugs = [p["slug"] for p in manifest["pages"]]
    assert slugs == ["quickstart", "orders"]
    for entry in manifest["pages"]:
        assert set(entry) >= {"file", "id", "order", "slug", "source_refs", "summary",
                              "title", "written_at"}
    assert ticks and ticks[-1] == (2, 2)                        # progress ends at (total, total)
    qs = (tmp_path / "quickstart.md").read_text()
    assert "[Orders](orders.md)" in qs


def test_noop_run_does_not_rewrite_manifest(seeded_conn, tmp_path, monkeypatch):
    monkeypatch.setattr(W, "load_pages", lambda: [SPEC])
    stub, _ = make_llm(seeded_conn)
    W.assemble_writer(seeded_conn, chat_fn=stub, model="m", out_dir=tmp_path, verbose=False)
    before = (tmp_path / "manifest.json").read_text()
    stub2, calls2 = make_llm(seeded_conn)
    W.assemble_writer(seeded_conn, chat_fn=stub2, model="m", out_dir=tmp_path, verbose=False)
    assert calls2["total"] == 0
    assert (tmp_path / "manifest.json").read_text() == before
