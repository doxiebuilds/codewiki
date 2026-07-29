"""Validators: citations resolve or get repaired/stripped; mermaid never ships broken;
thin/preambled/truncated pages are blocking."""

from codewiki.writer import validate as V

DET = '```mermaid\ngraph LR\n  a["pkg/a"]\n  b["pkg/b"]\n  a --> b\n```'


# ------------------------------------------------------------------ citations
def test_valid_citation_kept(seeded_conn):
    md = "claim `repo/pkg/orders.py:3` more"
    out, errors, stats = V.repair_citations(seeded_conn, md)
    assert "`repo/pkg/orders.py:3`" in out
    assert stats["invalid"] == 0


def test_rootless_path_gets_prefixed(seeded_conn):
    out, _, stats = V.repair_citations(seeded_conn, "see `pkg/orders.py:3`")
    assert "`repo/pkg/orders.py:3`" in out
    assert stats["repaired"] == 1


def test_out_of_range_line_dropped_but_path_kept(seeded_conn):
    out, _, stats = V.repair_citations(seeded_conn, "see `repo/pkg/orders.py:999`")
    assert "`repo/pkg/orders.py`" in out and ":999" not in out


def test_range_end_clamped(seeded_conn):
    out, _, _ = V.repair_citations(seeded_conn, "see `repo/pkg/orders.py:2-999`")
    assert ":2-" in out and "-999" not in out


def test_unknown_path_stripped(seeded_conn):
    out, _, stats = V.repair_citations(seeded_conn, "see `repo/no/such.py:1`")
    assert "such.py" not in out
    assert stats["invalid"] == 1


# ------------------------------------------------------------------ mermaid
def test_edited_architecture_block_substituted():
    md = f"# T\n\n## Architecture\n\n```mermaid\ngraph LR\n  x --> y\n```\ntext"
    out, warns, subs = V.check_and_fix_mermaid(md, DET, ["pkg/a", "pkg/b"])
    assert subs == 1 and DET in out and "x --> y" not in out


def test_bad_parens_extra_block_dropped():
    bad = "```mermaid\ngraph LR\n  a[calls foo(x)] --> b[ok]\n```"
    md = f"# T\n\n{DET}\n\nmore\n\n{bad}\n"
    out, warns, _ = V.check_and_fix_mermaid(md, DET, ["pkg/a", "pkg/b"])
    assert "foo(x)" not in out and DET in out


def test_missing_diagram_inserted():
    out, warns, subs = V.check_and_fix_mermaid("# T\n\nprose only", DET, [])
    assert DET in out and subs == 1


# ------------------------------------------------------------------ structure
def _valid_page(title="Orders", chars=4000):
    body = ("This system routes orders through validation before matching. " * 20 + "\n\n")
    md = (f"# {title}\n\n{body}## Architecture\n\nwalkthrough\n\n"
          f"## Order flow\n\n{body}## Persistence\n\n{body}"
          f"## Where to start & watch-outs\n\nstart at x.\n")
    return md + "p" * max(0, chars - len(md))


def test_valid_structure_passes():
    assert V.check_structure(_valid_page(), "Orders") == []


def test_thin_page_and_wrong_title_block():
    errors = V.check_structure("# Wrong\n\nshort", "Orders")
    joined = " ".join(errors)
    assert "start with exactly" in joined and "too thin" in joined


def test_preamble_and_truncation_block():
    md = "Here is the page:\n\n" + _valid_page() + "\n```python\ntruncated"
    errors = V.check_structure(md, "Orders")
    joined = " ".join(errors)
    assert "preamble" in joined and "unbalanced" in joined


def test_tidy_prose_skips_fences():
    md = "claim . done ,\n```mermaid\ngraph LR\n  a --> b .\n```\nmore ."
    out = V.tidy_prose(md)
    assert "claim. done," in out and "more." in out
    assert "a --> b ." in out            # fenced content untouched


# ------------------------------------------------------------------ full pipeline
def test_validate_page_end_to_end(seeded_conn):
    cites = " ".join(f"`repo/pkg/orders.py:{i}`" for i in range(1, 8))
    md = _valid_page() + f"\n\nEvidence: {cites}\n"
    report = V.validate_page(seeded_conn, "<think>hmm</think>" + md, "Orders", "", [])
    assert report.errors == []
    assert "<think>" not in report.markdown
    assert report.n_citations == 7
