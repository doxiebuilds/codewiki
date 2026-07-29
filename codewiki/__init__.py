"""
codewiki — deterministic-indexer + hierarchical-summary wiki pipeline.

Three decoupled stages (see build.py):

    [1] indexer   (NO LLM)  tree-sitter parse -> code-graph SQLite (symbols/edges/domain nodes)
    [2] generator (LLM only) hierarchical, hash-gated summaries fed *structured* context
    [3] assembly  (NO LLM)  Jinja2 render + graph-derived Mermaid -> docs/wiki/*.md + manifest.json

The LLM never navigates the repo; it only summarizes pre-extracted context. Citations and
diagrams are computed from the graph, so they cannot hallucinate. Change detection is
symbol-hash-exact, so a one-function edit regenerates ~4 small summaries, not whole pages.

Package layout uses absolute imports rooted at ``codewiki.*``; ``build.py`` puts the package's
parent dir on ``sys.path`` so that resolves regardless of the caller's cwd.
"""
