"""StatusReporter: null-object safety, atomic writes, stage-window pct math; summarize
on_progress monotonicity; count_stale_nodes as the progress denominator."""

import json

from codewiki import progress as P
from codewiki.generator import summarize as S
from codewiki.indexer import graph
from codewiki.indexer.discovery import FileMeta
from codewiki.store import db

from conftest import SEED_REL, SEED_SRC


def _read(path):
    return json.loads(path.read_text())


def test_null_object_writes_nothing(tmp_path):
    r = P.StatusReporter(None)
    r.start(); r.stage("summarize", total=10); r.tick(5, 10); r.done()
    assert not r.enabled
    assert list(tmp_path.iterdir()) == []


def test_lifecycle_and_pct_windows(tmp_path):
    path = tmp_path / "status.json"
    r = P.StatusReporter(path, run_id="r1", min_write_interval=0.0)
    r.start("boot")
    st = _read(path)
    assert st["state"] == "running" and st["stage"] == "index" and st["pct"] == 0.0
    assert st["run_id"] == "r1" and st["pid"] and st["started_at"]

    r.stage("summarize", total=100, detail="100 stale")
    assert _read(path)["pct"] == 5.0                     # window floor

    r.tick(50, 100)
    st = _read(path)
    assert st["pct"] == 5.0 + 0.5 * 65                   # halfway through 5-70
    assert st["counts"] == {"done": 50, "total": 100}

    r.stage("pages", total=15)
    assert _read(path)["pct"] == 70.0
    r.tick(15, 15)
    assert _read(path)["pct"] == 100.0

    r.done("15 pages")
    st = _read(path)
    assert st["state"] == "done" and st["pct"] == 100.0 and st["finished_at"]
    assert not (tmp_path / "status.json.tmp").exists()   # atomic: no temp leftover


def test_error_state(tmp_path):
    path = tmp_path / "s.json"
    r = P.StatusReporter(path, min_write_interval=0.0)
    r.start()
    r.error("LM Studio unavailable")
    st = _read(path)
    assert st["state"] == "error" and "LM Studio" in st["error"] and st["finished_at"]


def test_tick_zero_total_does_not_crash(tmp_path):
    r = P.StatusReporter(tmp_path / "s.json", min_write_interval=0.0)
    r.start()
    r.stage("summarize", total=0)
    r.tick(0, 0)
    assert _read(tmp_path / "s.json")["pct"] == 70.0     # empty stage jumps to its ceiling


# ------------------------------------------------------------------ summarize integration
def _fm(tmp_path, src):
    p = tmp_path / "demo.py"
    p.write_bytes(src)
    import hashlib
    return FileMeta(path=SEED_REL, abs=p, language="python",
                    sha256=hashlib.sha256(src).hexdigest(), size=len(src))


def test_summarize_on_progress_monotonic(tmp_path):
    conn = db.connect(tmp_path / "g.db")
    graph.index_file(conn, _fm(tmp_path, SEED_SRC))
    conn.commit()
    total = S.count_stale_nodes(conn, "m")
    assert total > 0
    ticks: list[tuple[int, int]] = []
    stub = lambda prompt, *, model=None, **kw: ('{"summary": "s"}',
                                                {"prompt_tokens": 1, "completion_tokens": 1})
    stats = S.summarize_all(conn, chat_fn=stub, model="m", only_stale=True,
                            on_progress=lambda d, t: ticks.append((d, t)))
    assert stats.generated == total
    assert [d for d, _ in ticks] == list(range(1, total + 1))    # monotonic, one per node
    assert ticks[-1] == (total, total)
    assert S.count_stale_nodes(conn, "m") == 0                   # everything fresh now
    assert S.count_stale_nodes(conn, "OTHER-MODEL") == total     # model swap → all stale
