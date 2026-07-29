"""
The load-bearing guarantee: editing one function re-summarizes only that function's branch
(function → module → package), not the whole file/page. Uses a stub LLM so it runs offline.
"""

import hashlib

from codewiki.indexer.discovery import FileMeta
from codewiki.indexer import graph
from codewiki.generator import summarize as S
from codewiki.store import db

SRC_V1 = b'''"""demo module."""

def alpha():
    return 1

def beta():
    return 2

class Foo:
    def method(self):
        return alpha()
'''

# only beta's body changes
SRC_V2 = SRC_V1.replace(b"return 2", b"return 22")

REL = "repo/pkg/demo.py"


def _fm(tmp_path, src):
    p = tmp_path / "demo.py"
    p.write_bytes(src)
    return FileMeta(path=REL, abs=p, language="python",
                    sha256=hashlib.sha256(src).hexdigest(), size=len(src))


def _stub_factory():
    calls = {"n": 0}

    def stub(prompt, *, model=None, **kw):
        calls["n"] += 1
        return '{"summary": "stub"}', {"prompt_tokens": 10, "completion_tokens": 5}

    return stub, calls


def test_one_function_edit_regenerates_only_its_branch(tmp_path):
    conn = db.connect(tmp_path / "g.db")
    graph.index_file(conn, _fm(tmp_path, SRC_V1))
    conn.commit()

    stub, calls = _stub_factory()
    run1 = S.summarize_all(conn, chat_fn=stub, only_stale=True)
    # module, alpha, beta, Foo(class), Foo.method, + package rollup
    assert run1.generated == 6, run1.by_kind
    assert run1.skipped == 0

    alpha_id = graph.symbol_id(REL, "alpha", "function")
    beta_id = graph.symbol_id(REL, "beta", "function")
    module_id = graph.symbol_id(REL, "", "module")
    alpha_hash_before = db.summary_hash(conn, alpha_id)

    # edit beta only, re-index that file, re-summarize
    graph.index_file(conn, _fm(tmp_path, SRC_V2))
    conn.commit()
    stub2, calls2 = _stub_factory()
    run2 = S.summarize_all(conn, chat_fn=stub2, only_stale=True)

    # exactly beta + its module + its package rollup — nothing else
    assert run2.generated == 3, run2.by_kind
    assert run2.by_kind == {"function": 1, "module": 1, "package": 1}
    assert calls2["n"] == 3

    # alpha and Foo.method summaries are untouched (same hash), beta's changed
    assert db.summary_hash(conn, alpha_id) == alpha_hash_before
    assert db.summary_hash(conn, module_id) is not None


def test_rerun_without_change_is_zero_llm_calls(tmp_path):
    conn = db.connect(tmp_path / "g.db")
    graph.index_file(conn, _fm(tmp_path, SRC_V1))
    conn.commit()
    stub, _ = _stub_factory()
    S.summarize_all(conn, chat_fn=stub, only_stale=True)

    stub2, calls2 = _stub_factory()
    again = S.summarize_all(conn, chat_fn=stub2, only_stale=True)
    assert again.generated == 0
    assert calls2["n"] == 0


def test_prompt_version_bump_invalidates_all(tmp_path, monkeypatch):
    """Prompt/model changes must retrigger summaries — staleness is not just content."""
    conn = db.connect(tmp_path / "g.db")
    graph.index_file(conn, _fm(tmp_path, SRC_V1))
    conn.commit()
    stub, _ = _stub_factory()
    S.summarize_all(conn, chat_fn=stub, only_stale=True)

    monkeypatch.setattr(S, "SUMMARY_PROMPT_VERSION", "s-test-bump")
    stub2, calls2 = _stub_factory()
    rerun = S.summarize_all(conn, chat_fn=stub2, only_stale=True)
    assert rerun.generated == 6 and rerun.skipped == 0     # every node went stale

    # same content + same version again -> fresh
    stub3, calls3 = _stub_factory()
    assert S.summarize_all(conn, chat_fn=stub3, only_stale=True).generated == 0


def test_model_change_invalidates_all(tmp_path):
    conn = db.connect(tmp_path / "g.db")
    graph.index_file(conn, _fm(tmp_path, SRC_V1))
    conn.commit()
    stub, _ = _stub_factory()
    S.summarize_all(conn, chat_fn=stub, only_stale=True, model="model-a")

    stub2, calls2 = _stub_factory()
    rerun = S.summarize_all(conn, chat_fn=stub2, only_stale=True, model="model-b")
    assert rerun.generated == 6 and calls2["n"] == 6
