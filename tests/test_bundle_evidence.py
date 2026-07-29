"""Evidence layer: stable eids under trim, test-file de-prioritization (+keep_tests bypass),
trivial-__init__ filtering, catalog/slice/allowed_cites consistency."""

from dataclasses import replace

from codewiki.assembly.pages import PageSpec
from codewiki.writer import bundle as B

from conftest import SEED_REL, SEED_TEST_REL

SPEC = PageSpec(slug="orders", title="Orders", order=1,
                include=["repo/pkg"], domain=["route"])
PKGS = ["repo/pkg", "repo/pkg/tests"]


def test_evidence_ids_and_catalog(seeded_conn):
    b = B.build_bundle(seeded_conn, SPEC, ["repo/pkg"])
    assert [it.eid for it in b.evidence] == [f"E{i+1}" for i in range(len(b.evidence))]
    catalog = B.evidence_catalog(b)
    for it in b.evidence:
        assert it.eid in catalog
    kinds = {it.kind for it in b.evidence}
    assert {"module", "symbol", "table"} <= kinds


def test_slice_and_allowed_cites(seeded_conn):
    b = B.build_bundle(seeded_conn, SPEC, ["repo/pkg"])
    mod = next(it for it in b.evidence if it.kind == "module")
    text = B.evidence_slice_text(b, [mod.eid])
    assert mod.eid in text and "LOCATIONS:" in text
    assert B.allowed_cites(b, [mod.eid]) == set(mod.cites)
    assert B.allowed_cites(b, []) == set()


def test_test_files_deprioritized_and_tagged(seeded_conn_with_tests):
    b = B.build_bundle(seeded_conn_with_tests, SPEC, PKGS)
    mod_paths = [p for p, _, _ in b.module_summaries]
    assert SEED_REL in mod_paths and SEED_TEST_REL in mod_paths
    assert mod_paths.index(SEED_REL) < mod_paths.index(SEED_TEST_REL)   # tests rank last
    test_items = [it for it in b.evidence
                  if it.kind == "module" and it.data[0] == SEED_TEST_REL]
    assert test_items and test_items[0].label.startswith("[module:test]")


def test_keep_tests_bypasses_the_cap_and_tag(seeded_conn_with_tests):
    spec = replace(SPEC, keep_tests=True)
    b = B.build_bundle(seeded_conn_with_tests, spec, PKGS)
    test_items = [it for it in b.evidence
                  if it.kind == "module" and it.data[0] == SEED_TEST_REL]
    assert test_items and not test_items[0].label.startswith("[module:test]")
    # test symbols are not capped: all 4 test functions may appear as key symbols
    test_syms = [s for s in b.key_symbols if s["file_path"] == SEED_TEST_REL]
    assert len(test_syms) >= 4


def test_trivial_init_modules_filtered(seeded_conn, tmp_path):
    import hashlib
    from codewiki.indexer import graph
    from codewiki.indexer.discovery import FileMeta
    src = b"# marker\n"
    p = tmp_path / "__init__.py"
    p.write_bytes(src)
    rel = "repo/pkg/__init__.py"
    graph.index_file(seeded_conn, FileMeta(path=rel, abs=p, language="python",
                                           sha256=hashlib.sha256(src).hexdigest(),
                                           size=len(src)))
    seeded_conn.commit()
    b = B.build_bundle(seeded_conn, SPEC, ["repo/pkg"])
    assert rel not in [path for path, _, _ in b.module_summaries]
    assert all(rel not in c for c in b.citation_index)


def test_trim_preserves_surviving_eids(seeded_conn):
    b = B.build_bundle(seeded_conn, SPEC, ["repo/pkg"])
    before = {it.eid: it.kind for it in b.evidence}
    trimmed = B.trim_to_budget(b, 50)                     # absurdly small budget
    for it in trimmed.evidence:
        assert before[it.eid] == it.kind                  # ids stable, kinds unchanged
    # views stay consistent with surviving evidence
    surviving_modules = {it.data[0] for it in trimmed.evidence if it.kind == "module"}
    assert {p for p, _, _ in trimmed.module_summaries} == surviving_modules
    # pkg items are never trimmed
    assert [it for it in trimmed.evidence if it.kind == "pkg"] == \
           [it for it in b.evidence if it.kind == "pkg"]
