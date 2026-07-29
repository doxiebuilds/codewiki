"""Cross-file call/import resolution: import-aware first, unique-name fallback, never guess."""

import hashlib

from codewiki.indexer import graph, resolve
from codewiki.indexer.discovery import FileMeta
from codewiki.store import db

HELPERS = b'''"""helpers module."""

def validate(o):
    """Validate an order."""
    return bool(o)

def shared_name():
    return 1
'''

ORDERS = b'''"""orders module."""
from pkg.helpers import validate

def submit_order(o):
    return validate(o) and totally_unique_fn(o)

def shared_name():
    return 2
'''

UNIQUE = b'''"""elsewhere."""

def totally_unique_fn(o):
    return o
'''


def _index(conn, tmp_path, rel, src):
    p = tmp_path / rel.replace("/", "_")
    p.write_bytes(src)
    graph.index_file(conn, FileMeta(path=rel, abs=p, language="python",
                                    sha256=hashlib.sha256(src).hexdigest(), size=len(src)))


def test_import_and_unique_resolution(tmp_path):
    conn = db.connect(tmp_path / "g.db")
    _index(conn, tmp_path, "repo/pkg/helpers.py", HELPERS)
    _index(conn, tmp_path, "repo/pkg/orders.py", ORDERS)
    _index(conn, tmp_path, "repo/other/unique.py", UNIQUE)
    conn.commit()

    stats = resolve.resolve_all(conn)
    assert stats.imports_resolved == 1

    # the `from pkg.helpers import validate` edge points at helpers' module symbol
    imp = conn.execute(
        "SELECT dst_id, resolved FROM edges WHERE kind='imports' AND dst_name='pkg.helpers'"
    ).fetchone()
    assert imp["dst_id"] == "repo/pkg/helpers.py::<module>::module"
    assert imp["resolved"] == "import"

    # validate() resolves via the import (helpers is imported by orders)
    call = conn.execute(
        "SELECT dst_id, resolved FROM edges WHERE kind='calls' AND dst_name='validate'").fetchone()
    assert call["resolved"] == "import"
    assert call["dst_id"].startswith("repo/pkg/helpers.py::validate")

    # totally_unique_fn() resolves by repo-wide uniqueness (its file is NOT imported)
    call = conn.execute(
        "SELECT dst_id, resolved FROM edges WHERE kind='calls' AND dst_name='totally_unique_fn'"
    ).fetchone()
    assert call["resolved"] == "unique"
    assert call["dst_id"].startswith("repo/other/unique.py")


def test_ambiguous_names_stay_dangling(tmp_path):
    conn = db.connect(tmp_path / "g.db")
    _index(conn, tmp_path, "repo/pkg/helpers.py", HELPERS)
    _index(conn, tmp_path, "repo/pkg/orders.py", ORDERS)
    conn.commit()
    caller = b'"""caller."""\n\ndef go():\n    return shared_name()\n'
    _index(conn, tmp_path, "repo/x/caller.py", caller)
    conn.commit()

    resolve.resolve_all(conn)
    # shared_name is defined in two files neither of which caller imports -> dangling
    call = conn.execute(
        "SELECT dst_id, resolved FROM edges WHERE kind='calls' AND dst_name='shared_name' "
        "AND src_id LIKE 'repo/x/caller.py%'").fetchone()
    assert call["dst_id"] is None and call["resolved"] == ""


def test_relative_import_candidates():
    cands = resolve._python_candidates("..storage.driver", "repo/a/b/mod.py")
    assert "repo/a/storage/driver.py" in cands
    cands = resolve._python_candidates("import apps.api.main", "x.py")
    assert "repo/apps/api/main.py" in cands


def test_rust_use_candidates():
    cands = resolve._rust_candidates(
        "use crate::engine::thread_pool::ThreadPool;",
        "repo/engine_core/src/lib.rs")
    assert "repo/engine_core/src/engine/thread_pool.rs" in cands
    assert "repo/engine_core/src/engine/thread_pool/mod.rs" in cands


def test_ts_relative_candidates():
    cands = resolve._ts_candidates("../shared/api", "repo/web/app/src/features/chart/x.ts")
    assert "repo/web/app/src/features/shared/api.ts" in cands
