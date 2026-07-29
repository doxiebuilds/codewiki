#!/usr/bin/env python3
"""
build.py — codewiki CLI: deterministic index -> hierarchical summaries -> assembled wiki.

    init       [--force] [--out PATH]   write a starter pages.yaml matching this repo's layout
    index      [--full]                 (re)build the code-graph DB from tree-sitter (no LLM)
    summarize  [--limit N] [--kinds ..] hash-gated hierarchical summaries via a local LLM
    calibrate  [--sample N]             summarize a pinned 10-node sample into a review dir
    assemble   [--writer|--legacy] [--no-llm] [--pages a,b] [--out-dir D] [--force] [--prune]
                                        write docs/wiki/*.md + manifest.json
    update     [--since-ref REF]        incremental index -> summarize stale -> assemble (pre-push)
    status                              index/summary/page freshness + token totals

LLM stages: `summarize` (per-node), `calibrate` (sample of summarize) and the `assemble --writer`
page writer (one narrative page per stale page, validated against the graph). All hit the same
local OpenAI-compatible server (LM Studio, Ollama, vLLM, ...; see codewiki/llm.py), and all are
non-blocking: if the server is down they are skipped and the deterministic parts still run.
Editing one function re-summarizes only that function's branch and rewrites only the page(s)
containing it, so `update` is cheap.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent                  # …/codewiki (package parent)
sys.path.insert(0, str(HERE.parent))                    # enables `import codewiki` from any cwd

from codewiki import config as CFG                       # noqa: E402
from codewiki import progress as P                       # noqa: E402
from codewiki.store import db                            # noqa: E402
from codewiki.indexer.run import run_index               # noqa: E402
from codewiki.generator import summarize as S            # noqa: E402
from codewiki.generator import calibrate as C            # noqa: E402
from codewiki.assembly import render, scaffold           # noqa: E402
from codewiki.assembly.pages import BUNDLED_EXAMPLE, default_pages_path, load_pages  # noqa: E402
from codewiki.paths import GRAPH_DB                       # noqa: E402
from codewiki.writer import write as W                   # noqa: E402
from codewiki.llm import DEFAULT_MODEL, lmstudio_up, model_available  # noqa: E402


def _cmd_init(args) -> int:
    conn = db.connect()
    dest = Path(args.out) if args.out else (CFG.load().repo_root / "pages.yaml")
    if dest.exists() and not args.force:
        print(f"{dest} already exists — pass --force to overwrite it.")
        return 1
    pages = scaffold.propose(conn)
    if not pages:
        print("the code graph has no packages yet — run `codewiki index --full` first.")
        return 1
    dest.write_text(scaffold.render_yaml(pages), encoding="utf-8")
    print(f"init: wrote {dest} ({len(pages)} pages)")
    for p in pages:
        print(f"  {p['slug']:<18} {', '.join(p['include'])}")
    print("\nEdit it to taste, then run `codewiki assemble --writer`.")
    return 0


def _explain_zero_pages(conn) -> None:
    """Assemble wrote nothing. Say why, and what to do about it."""
    specs = load_pages()
    prefixes = sorted({p for s in specs for p in s.include})
    source = default_pages_path()
    origin = ("the bundled starter taxonomy" if source == BUNDLED_EXAMPLE else str(source))
    print("\nNo pages were written — no indexed package matched any `include` prefix.")
    print(f"  taxonomy:       {origin}")
    print(f"  prefixes tried: {', '.join(prefixes) or '(none)'}")

    counts = scaffold.package_counts(conn)
    if not counts:
        print("  the code graph has no packages — run `codewiki index --full` first.")
        return
    top = scaffold.roots(counts) or sorted(counts)[:5]
    print(f"  this repo has:  {', '.join(top[:8])}")
    print("\nFix: run `codewiki init` to generate a pages.yaml matching this repo, then re-run.")


def _cmd_index(args) -> int:
    conn = db.connect()
    stats = run_index(conn, only_changed=not args.full)
    print(f"index: {stats.files_total} files, {stats.symbols_total} symbols "
          f"(+{len(stats.added)} ~{len(stats.modified)} -{len(stats.removed)})")
    if stats.domain:
        print("  domain nodes:", ", ".join(f"{k}={v}" for k, v in stats.domain.items()))
    if stats.resolution:
        r = stats.resolution
        print(f"  edges: imports {r.imports_resolved}/{r.imports_total} resolved; calls "
              f"local={r.calls_local} import={r.calls_import} unique={r.calls_unique} "
              f"dangling={r.calls_unresolved}")
    return 0


def _llm_ready(model: str) -> bool:
    if not lmstudio_up():
        print("LM Studio not reachable — skipping LLM step (deterministic parts still ran).")
        return False
    if not model_available(model):
        print(f"model {model} not loaded in LM Studio — skipping LLM step.")
        return False
    return True


def _cmd_summarize(args) -> int:
    conn = db.connect()
    if not _llm_ready(args.model):
        return 0
    kinds = set(args.kinds.split(",")) if args.kinds else None
    stats = S.summarize_all(conn, model=args.model, only_stale=not args.all, limit=args.limit,
                            kinds=kinds, verbose=True, concurrency=args.concurrency)
    print(f"summarize: generated={stats.generated} skipped={stats.skipped} failed={stats.failed} "
          f"tokens_in={stats.tokens_in} tokens_out={stats.tokens_out}")
    print("  by_kind:", stats.by_kind)
    return 0


def _cmd_assemble(args) -> int:
    conn = db.connect()
    chat_fn = None if args.no_llm else (S.lmstudio_chat if _llm_ready(args.model) else None)
    if args.writer and chat_fn is not None:
        manifest = W.assemble_writer(
            conn, chat_fn=chat_fn, model=args.model, force=args.force,
            only=set(args.pages.split(",")) if args.pages else None,
            out_dir=Path(args.out_dir) if args.out_dir else None, prune=args.prune)
    else:
        if args.writer:
            print("page writer needs the LLM — falling back to the deterministic renderer.")
        manifest = render.assemble(conn, chat_fn=chat_fn,
                                   model=args.model if chat_fn else "codewiki-deterministic")
    print(f"assemble: wrote {manifest['page_count']} pages + manifest.json")
    if manifest["page_count"] == 0:
        _explain_zero_pages(conn)
    return 0


def _cmd_calibrate(args) -> int:
    conn = db.connect()
    if not _llm_ready(args.model):
        return 1
    results = C.run_calibration(conn, model=args.model, n=args.sample,
                                reset_set=args.reset_set)
    out = C.CALIBRATION_DIR / S.SUMMARY_PROMPT_VERSION
    print(f"calibrate [{S.SUMMARY_PROMPT_VERSION}]: {len(results['nodes'])} summaries, "
          f"{len(results['failed'])} failed -> {out}")
    for f in results["failed"]:
        print(f"  FAILED {f['id']}: {f['error']}")
    return 0


# --------------------------------------------------------------- update lock (single-flight)
# Covers BOTH entry points that can run `update` concurrently — a manual `codewiki update` and
# a dashboard-triggered refresh — since the API's own 409 check only guards button-vs-button.
UPDATE_LOCK = None  # set lazily; STATE_DIR import happens at module load below


def _lock_path() -> Path:
    from codewiki.paths import STATE_DIR
    return STATE_DIR / "update.lock"


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError, OSError):
        return False


def _acquire_update_lock() -> bool:
    lock = _lock_path()
    lock.parent.mkdir(parents=True, exist_ok=True)
    if lock.exists():
        try:
            pid = int(lock.read_text().strip() or "0")
        except ValueError:
            pid = 0
        if pid and _pid_alive(pid):
            return False
        # stale lock (dead pid) — take over
    lock.write_text(str(os.getpid()), encoding="utf-8")
    return True


def _release_update_lock() -> None:
    try:
        _lock_path().unlink(missing_ok=True)
    except OSError:
        pass


def _cmd_update(args) -> int:
    reporter = P.StatusReporter(args.status_file)
    if not _acquire_update_lock():
        print("another codewiki update is already running (STATE_DIR/update.lock) — aborting.")
        reporter.error("another codewiki update is already running")
        return 3
    try:
        reporter.start()
        conn = db.connect()
        reporter.stage("index")
        idx = run_index(conn, only_changed=True)
        print(f"index: {idx.symbols_total} symbols (+{len(idx.added)} ~{len(idx.modified)} -{len(idx.removed)})")
        chat_fn = None
        if _llm_ready(args.model):
            total = S.count_stale_nodes(conn, args.model)
            reporter.stage("summarize", total=total,
                           detail=f"{total} stale nodes to summarize")
            st = S.summarize_all(conn, model=args.model, only_stale=True, verbose=True,
                                 on_progress=reporter.tick)
            print(f"summarize: generated={st.generated} skipped={st.skipped} tokens_out={st.tokens_out}")
            chat_fn = S.lmstudio_chat
        elif reporter.enabled:
            # button-triggered run: a silent deterministic fallback would masquerade as success
            reporter.error("LM Studio at localhost:1234 unavailable or model not loaded")
            return 2
        if chat_fn is not None and not args.legacy:
            reporter.stage("pages", total=len(load_pages()) + 1, detail="writing stale pages")
            manifest = W.assemble_writer(conn, chat_fn=chat_fn, model=args.model,
                                         since_ref=args.since_ref or "",
                                         on_progress=reporter.tick)
        else:
            reporter.stage("pages", total=1)
            manifest = render.assemble(conn, chat_fn=chat_fn,
                                       model=args.model if chat_fn else "codewiki-deterministic")
        print(f"assemble: wrote {manifest['page_count']} pages + manifest.json")
        if manifest["page_count"] == 0:
            _explain_zero_pages(conn)
        reporter.done(f"{manifest['page_count']} pages")
        return 0
    except Exception as exc:
        reporter.error(str(exc))
        raise
    finally:
        _release_update_lock()


def _cmd_status(args) -> int:
    if not GRAPH_DB.exists():
        print("no code-graph DB yet — run `build.py index --full`.")
        return 0
    conn = db.connect()
    files = conn.execute("SELECT COUNT(*) n FROM files").fetchone()["n"]
    syms = conn.execute("SELECT COUNT(*) n FROM symbols").fetchone()["n"]
    summ = conn.execute("SELECT COUNT(*) n FROM summaries").fetchone()["n"]
    toks = conn.execute("SELECT COALESCE(SUM(tokens_in),0) i, COALESCE(SUM(tokens_out),0) o FROM summaries").fetchone()
    stale = S.count_stale_nodes(conn, args.model)
    print(f"indexed_at : {db.get_meta(conn, 'indexed_at', '(never)')}  head={db.get_meta(conn,'head','')[:8]}")
    print(f"files      : {files}")
    print(f"symbols    : {syms}")
    print(f"summaries  : {summ}  (tokens in={toks['i']} out={toks['o']}, "
          f"prompt={S.SUMMARY_PROMPT_VERSION}, model={args.model})")
    print(f"stale nodes: {stale}  (need summarize)")

    builds = {r["slug"]: r for r in conn.execute("SELECT * FROM page_builds")}
    if builds:
        print("pages      :")
        for spec in load_pages():
            b = builds.get(spec.slug)
            if b is None:
                print(f"  {spec.slug:28s} (never written)")
                continue
            pkgs = render._packages_for(conn, spec.include)
            fresh = pkgs and b["hash"] == W.page_writer_hash(conn, spec, pkgs, b["model"])
            print(f"  {spec.slug:28s} {'fresh' if fresh else 'STALE'}  {b['status']} "
                  f"at {b['written_at'][:19]} in={b['tokens_in']} out={b['tokens_out']}")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="codewiki", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    pn = sub.add_parser("init", help="write a starter pages.yaml matching this repo's layout")
    pn.add_argument("--force", action="store_true", help="overwrite an existing pages.yaml")
    pn.add_argument("--out", default=None, help="write here instead of <repo-root>/pages.yaml")
    pn.set_defaults(fn=_cmd_init)

    pi = sub.add_parser("index", help="build the code-graph DB (no LLM)")
    pi.add_argument("--full", action="store_true", help="reindex every file, not just changed")
    pi.set_defaults(fn=_cmd_index)

    ps = sub.add_parser("summarize", help="hash-gated hierarchical summaries (LM Studio)")
    ps.add_argument("--model", default=DEFAULT_MODEL)
    ps.add_argument("--limit", type=int, default=None, help="stop after N generations")
    ps.add_argument("--kinds", default=None, help="comma list: function,method,class,module,package")
    ps.add_argument("--all", action="store_true", help="regenerate even fresh nodes")
    ps.add_argument("--concurrency", type=int, default=1,
                    help="parallel LLM calls within a level (DB writes stay serialized)")
    ps.set_defaults(fn=_cmd_summarize)

    pc = sub.add_parser("calibrate", help="summarize a pinned sample into a review dir (no DB writes)")
    pc.add_argument("--model", default=DEFAULT_MODEL)
    pc.add_argument("--sample", type=int, default=10, help="sample size when creating the set")
    pc.add_argument("--reset-set", action="store_true", help="re-pick the pinned calibration nodes")
    pc.set_defaults(fn=_cmd_calibrate)

    pa = sub.add_parser("assemble", help="render docs/wiki/*.md + manifest.json")
    pa.add_argument("--model", default=DEFAULT_MODEL)
    pa.add_argument("--no-llm", action="store_true", help="deterministic page overviews only")
    pa.add_argument("--writer", action="store_true",
                    help="LLM page writer (narrative pages, validated citations/diagrams)")
    pa.add_argument("--force", action="store_true", help="regenerate even fresh pages")
    pa.add_argument("--pages", default=None, help="comma list of slugs (writer only)")
    pa.add_argument("--out-dir", default=None,
                    help="write pages here instead of docs/wiki (preview mode)")
    pa.add_argument("--prune", action="store_true",
                    help="delete .md files not in the new manifest (cutover)")
    pa.set_defaults(fn=_cmd_assemble)

    pu = sub.add_parser("update", help="incremental index -> summarize stale -> assemble")
    pu.add_argument("--model", default=DEFAULT_MODEL)
    pu.add_argument("--since-ref", default=None, help="git ref for page change context")
    pu.add_argument("--legacy", action="store_true", help="use the Jinja renderer, not the writer")
    pu.add_argument("--status-file", default=os.environ.get("CODEWIKI_STATUS_PATH") or None,
                    help="write machine-readable refresh progress JSON here (for a consuming UI)")
    pu.set_defaults(fn=_cmd_update)

    pst = sub.add_parser("status", help="index/summary/page freshness + token totals")
    pst.add_argument("--model", default=DEFAULT_MODEL)
    pst.set_defaults(fn=_cmd_status)

    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
