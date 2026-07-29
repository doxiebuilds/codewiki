-- codewiki code-graph schema (deterministic index output; no LLM).
--
-- The unit of incremental change is a hash:
--   * leaf symbols (function/method): content_hash = sha256(exact source span bytes)
--   * containers (class/module/package/page): rollup_hash = sha256(signature + sorted child hashes)
-- A summary is stored keyed by the hash it was generated for; regeneration is needed iff the
-- current hash != the stored summary's hash. Everything here is computed without an LLM.

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS files (
    path      TEXT PRIMARY KEY,     -- repo-root-relative
    language  TEXT NOT NULL,
    sha256    TEXT NOT NULL,
    size      INTEGER NOT NULL,
    git_hash  TEXT,
    n_symbols INTEGER NOT NULL DEFAULT 0
);

-- One row per module (file), class, function, method.
CREATE TABLE IF NOT EXISTS symbols (
    id           TEXT PRIMARY KEY,   -- "<path>::<qualname>::<kind>" (stable across runs)
    file_path    TEXT NOT NULL REFERENCES files(path) ON DELETE CASCADE,
    kind         TEXT NOT NULL,      -- module|class|function|method
    name         TEXT NOT NULL,
    qualname     TEXT NOT NULL,      -- dotted within file (e.g. Foo.bar); "" for module
    parent_id    TEXT,               -- enclosing symbol id (NULL for module)
    package      TEXT NOT NULL,      -- owning directory, repo-relative (grouping key)
    start_line   INTEGER NOT NULL,
    end_line     INTEGER NOT NULL,
    signature    TEXT NOT NULL DEFAULT '',
    docstring    TEXT NOT NULL DEFAULT '',
    decorators   TEXT NOT NULL DEFAULT '[]',   -- json array
    content_hash TEXT NOT NULL,      -- sha256 of exact source span (leaf change unit)
    rollup_hash  TEXT NOT NULL       -- container hash (== content_hash for leaves)
);
CREATE INDEX IF NOT EXISTS idx_symbols_file    ON symbols(file_path);
CREATE INDEX IF NOT EXISTS idx_symbols_parent  ON symbols(parent_id);
CREATE INDEX IF NOT EXISTS idx_symbols_package ON symbols(package);
CREATE INDEX IF NOT EXISTS idx_symbols_kind    ON symbols(kind);

-- Directed edges between symbols and/or raw names (unresolved callees keep dst_id NULL).
CREATE TABLE IF NOT EXISTS edges (
    src_id   TEXT NOT NULL,
    kind     TEXT NOT NULL,          -- calls|imports|contains|publishes|consumes
    dst_id   TEXT,                   -- resolved target symbol id (NULL if external/unresolved)
    dst_name TEXT NOT NULL,          -- raw callee/import/channel text as written
    resolved TEXT NOT NULL DEFAULT ''  -- how dst_id was found: local|import|unique|'' (dangling)
);
CREATE INDEX IF NOT EXISTS idx_edges_src ON edges(src_id, kind);
CREATE INDEX IF NOT EXISTS idx_edges_dst ON edges(dst_id, kind);

-- Repo-specific typed nodes (routes, redis channels, services/tiers, db tables, ws events).
CREATE TABLE IF NOT EXISTS domain_nodes (
    id        TEXT PRIMARY KEY,
    kind      TEXT NOT NULL,         -- route|redis_channel|service|db_table|ws_event
    name      TEXT NOT NULL,
    detail    TEXT NOT NULL DEFAULT '{}',  -- json (method/tier/etc.)
    file_path TEXT,
    line      INTEGER
);
CREATE INDEX IF NOT EXISTS idx_domain_kind ON domain_nodes(kind);

-- Page-writer build records: one row per wiki page written by the LLM page writer. `hash` is the
-- page_writer_hash (content rollups + prompt version + model + spec) the page was generated for;
-- a page is fresh iff its current hash matches. Failed/fallback builds store NO hash so they
-- retry on the next run.
CREATE TABLE IF NOT EXISTS page_builds (
    slug           TEXT PRIMARY KEY,
    hash           TEXT NOT NULL,
    git_head       TEXT NOT NULL DEFAULT '',
    model          TEXT NOT NULL DEFAULT '',
    status         TEXT NOT NULL DEFAULT '',   -- written|fallback_jinja
    tokens_in      INTEGER NOT NULL DEFAULT 0,
    tokens_out     INTEGER NOT NULL DEFAULT 0,
    validator_json TEXT NOT NULL DEFAULT '{}',
    meta_json      TEXT NOT NULL DEFAULT '{}', -- the manifest entry for this page
    written_at     TEXT NOT NULL DEFAULT '',
    skeleton_json  TEXT NOT NULL DEFAULT '{}'  -- the validated page plan (planner output)
);

-- Hierarchical summaries (the ONLY LLM output). node_id references a symbol id, a package key
-- ("pkg::<path>"), or a page key ("page::<slug>"). Regenerate iff hash != symbol's current hash.
CREATE TABLE IF NOT EXISTS summaries (
    node_id      TEXT PRIMARY KEY,
    node_kind    TEXT NOT NULL,      -- symbol|package|page
    hash         TEXT NOT NULL,      -- the content/rollup hash this summary was generated for
    summary_json TEXT NOT NULL,      -- json {purpose,inputs,outputs,side_effects,summary}
    model        TEXT NOT NULL DEFAULT '',
    tokens_in    INTEGER NOT NULL DEFAULT 0,
    tokens_out   INTEGER NOT NULL DEFAULT 0,
    generated_at TEXT NOT NULL DEFAULT ''
);
