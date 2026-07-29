"""The deterministic architecture anchor: layer classification, generic-name filtering, and the
layered `flowchart TD` render (subgraphs, cross-boundary edges, channel cylinders, classDefs)."""

from pathlib import Path

import pytest

from codewiki import config as C
from codewiki.assembly import diagrams as D
from codewiki.store import db

ROOT = "repo/"

# A representative 7-layer taxonomy (see config.py's [[diagram.layers]]) so these tests exercise
# real layer classification/rendering rather than the config-less single-"support"-bucket default.
_LAYERS = (
    C.DiagramLayer("ingest", "Ingestion", "ingest",
                   ("ingest/runtime", "ingest", "apps/ingest_scanner")),
    C.DiagramLayer("rust", "Rust Engine", "rust", ("engine_core",)),
    C.DiagramLayer("compute", "Domain & Compute", "compute", ("domain", "packages/analytics")),
    C.DiagramLayer("store", "Storage & Pub/Sub", "store", ("packages/storage",)),
    C.DiagramLayer("api", "API", "api", ("apps/api",)),
    C.DiagramLayer("ui", "UI", "ui", ("web/app",)),
    C.DiagramLayer("jobs", "Jobs", "jobs", ("jobs/",)),
)


@pytest.fixture(autouse=True)
def _layered_config(monkeypatch):
    cfg = C.Config(repo_root=Path("/repo"), source_subdir="repo",
                   github_blob_base="https://github.com/example/repo/blob/main/",
                   diagram_layers=_LAYERS)
    monkeypatch.setattr(C, "load", lambda: cfg)


# ------------------------------------------------------------------ pure helpers
def test_layer_classification():
    assert D._layer_of(ROOT + "ingest/runtime") == "ingest"
    assert D._layer_of(ROOT + "engine_core/src/engine") == "rust"
    assert D._layer_of(ROOT + "domain/calculations") == "compute"
    assert D._layer_of(ROOT + "packages/storage") == "store"
    assert D._layer_of(ROOT + "apps/api/runtime") == "api"
    assert D._layer_of(ROOT + "web/app/src") == "ui"
    assert D._layer_of(ROOT + "jobs/maintenance") == "jobs"
    assert D._layer_of(ROOT + "packages/weird_thing") == "support"


def test_meaningful_name_filter():
    # generic builtins / container ops / dunders → never trusted as edges or labels
    for junk in ("items", "round", "keys", "values", "copy", "close", "remove", "get",
                 "commit", "monotonic", "_private", ""):
        assert not D._meaningful(junk), junk
    # domain-specific names → kept
    for good in ("process_event", "fetch_reports", "PaymentGateway", "detect_patterns_for_window",
                 "reconcile_once", "broadcast"):
        assert D._meaningful(good), good


def test_label_is_short_component_not_full_path():
    assert D._label(ROOT + "apps/api/runtime") == "api/runtime"
    assert D._label(ROOT + "engine_core") == "engine_core"


def test_emit_is_flowchart_with_subgraphs_and_classes():
    node_layer = {"A": "ingest", "B": "rust", "CH": "store"}
    node_label = {"A": "runtime", "B": "engine", "CH": "jobs:completed"}
    node_shape = {"A": ('["', '"]'), "B": ('["', '"]'), "CH": ('[("', '")]')}
    node_class = {"A": "ingest", "B": "rust", "CH": "infra"}
    edges = [("A", "B", "process_event()"), ("B", "CH", "jobs:completed")]
    out = D._emit(node_layer, node_label, node_shape, node_class, edges)
    assert out.startswith("```mermaid\nflowchart TD")
    assert "graph LR" not in out and "graph TD" not in out
    assert 'subgraph ingest_grp["Ingestion' in out
    assert 'CH[("jobs:completed")]' in out                 # cylinder shape preserved
    assert 'A -- "process_event()" --> B' in out
    assert "classDef infra" in out and "class CH infra;" in out
    # only classDefs actually used are emitted
    assert "classDef ui" not in out


# ------------------------------------------------------------------ synthetic graph
def _sym(conn, sid, path, pkg, kind, name, qual=""):
    conn.execute("INSERT OR IGNORE INTO files(path,language,sha256,size,n_symbols) "
                 "VALUES(?,?,?,?,1)", (path, "python", sid, 1))
    conn.execute(
        "INSERT INTO symbols(id,file_path,kind,name,qualname,package,start_line,end_line,"
        "signature,content_hash,rollup_hash) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (sid, path, kind, name, qual, pkg, 1, 20, name, sid, sid))


def _graph(tmp_path):
    conn = db.connect(tmp_path / "g.db")
    ing = ROOT + "ingest/runtime"
    rust = ROOT + "engine_core/src"
    api = ROOT + "apps/api/runtime"
    _sym(conn, "ing::mod", ing + "/live.py", ing, "module", "live")
    _sym(conn, "ing::fn", ing + "/live.py", ing, "function", "run", "run")
    _sym(conn, "rust::mod", rust + "/lib.rs", rust, "module", "lib")
    _sym(conn, "rust::evt", rust + "/lib.rs", rust, "function", "process_event", "process_event")
    _sym(conn, "api::mod", api + "/ws.py", api, "module", "ws")
    _sym(conn, "api::items", api + "/ws.py", api, "function", "items", "items")

    def edge(src, kind, dst_id, dst_name, resolved=""):
        conn.execute("INSERT INTO edges(src_id,kind,dst_id,dst_name,resolved) VALUES(?,?,?,?,?)",
                     (src, kind, dst_id, dst_name, resolved))

    edge("ing::mod", "imports", "rust::mod", "engine_core.src", "import")   # backbone
    edge("ing::fn", "calls", "rust::evt", "process_event", "import")           # meaningful → label
    edge("ing::fn", "calls", "api::items", "items", "import")               # generic → DROPPED
    edge("ing::fn", "publishes", None, "jobs:completed", "")                # channel cylinder
    conn.commit()
    return conn, {"ing": ing, "rust": rust, "api": api}


def test_full_diagram_flowchart_cross_boundary_and_channel(tmp_path):
    conn, pk = _graph(tmp_path)
    # page owns ingest + rust; the api package exists only as a (generic, to-be-dropped) callee
    out = D.package_dependency_mermaid(conn, {pk["ing"], pk["rust"]})
    assert out.startswith("```mermaid\nflowchart TD")
    assert "graph LR" not in out
    assert 'subgraph ingest_grp[' in out and 'subgraph rust_grp[' in out
    # meaningful cross-boundary call is drawn and labelled; generic call is gone
    assert "process_event()" in out
    assert "apps_api" not in out and "items" not in out
    # Redis channel becomes an infra cylinder in the store layer
    assert 'jobs:completed")]' in out
    assert "classDef infra" in out


def test_generic_only_link_does_not_fabricate_a_node(tmp_path):
    conn, pk = _graph(tmp_path)
    out = D.package_dependency_mermaid(conn, {pk["ing"], pk["rust"]})
    # the ONLY path to the api package was a generic `.items()` call → api never appears
    assert "api_grp" not in out
