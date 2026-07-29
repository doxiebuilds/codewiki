"""
write.py — the multi-step page-writer orchestrator (plan → fill sections → diagrams →
assemble → validate → store).

Per page: hash-gate (content rollups + prompt version + model + spec incl. keep_tests) → build
the evidence bundle → ONE planner call (skeleton; retry once; deterministic fallback) → one
fill call per section, all sharing a byte-identical prompt prefix (LM Studio prefix cache) →
diagram calls for the planned flow/sequence slots → deterministic assembly + page checks +
GitHub Sources linkification. A section that fails twice degrades to its deterministic
fallback, never sinking the page; a page where >1/3 of sections fell back (or page-level
checks fail) is still written but stores NO hash, so it self-heals next run. The Jinja page
(`assembly/render.py`) remains only for transport-level failure (LLM unreachable).

``chat_fn`` is injected everywhere (tests run the whole flow with scripted mock LLMs).
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from codewiki.llm import LMSTUDIO_BASE_URL

from codewiki.assembly import render
from codewiki.assembly.pages import PageSpec, load_pages
from codewiki.paths import PAGE_MANIFEST, WIKI_DIR
from codewiki.store import db
from codewiki.writer import bundle as B
from codewiki.writer import diagram as D
from codewiki.writer import gitlog, prompts, sections, skeleton, validate
from codewiki.writer.pointer import ensure_pointer_section

PLANNER_MAX_TOKENS = 2200   # qwen pretty-prints JSON (~1 token/line-item); 900 truncated plans
SECTION_MAX_TOKENS = 1400
DIAGRAM_MAX_TOKENS = 700
QUICKSTART_MAX_TOKENS = 700
DEFAULT_BUDGET = 24000
MIN_BUDGET = 8000          # below this the bundle is too degraded — refuse, use Jinja
SAFETY_MARGIN = 1500
CALL_TIMEOUT = 900
MAX_FALLBACK_RATIO = 1 / 3  # more than this many fallback sections ⇒ page stays stale


@dataclass
class WriteResult:
    slug: str
    status: str                                   # written|skipped_fresh|fallback_jinja|skipped
    tokens_in: int = 0
    tokens_out: int = 0
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    fresh: bool = True                            # False ⇒ no hash stored, retried next run


# ------------------------------------------------------------------ hashing / budget
def page_writer_hash(conn: sqlite3.Connection, spec: PageSpec, packages: list[str],
                     model: str) -> str:
    base = render._page_hash(conn, spec, packages)
    spec_digest = json.dumps({"slug": spec.slug, "title": spec.title, "include": spec.include,
                              "domain": spec.domain, "keep_tests": spec.keep_tests},
                             sort_keys=True)
    joined = f"{base}|{prompts.WRITER_PROMPT_VERSION}|{model}|{spec_digest}"
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def context_budget(model: str, max_tokens: int = SECTION_MAX_TOKENS) -> int:
    """Input-token budget from LM Studio's loaded context length (env override CODEWIKI_CTX)."""
    ctx_len = 0
    env = os.environ.get("CODEWIKI_CTX")
    if env and env.isdigit():
        ctx_len = int(env)
    else:
        try:  # LM Studio native REST reports the loaded context length
            root = LMSTUDIO_BASE_URL.rsplit("/v1", 1)[0]
            with urllib.request.urlopen(f"{root}/api/v0/models", timeout=2) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            for item in data.get("data", []):
                if item.get("id") == model:
                    ctx_len = int(item.get("loaded_context_length") or 0)
                    break
        except Exception:
            ctx_len = 0
    if not ctx_len:
        ctx_len = 32768
    return min(DEFAULT_BUDGET, ctx_len - max_tokens - SAFETY_MARGIN)


def _dump_debug(debug_dir: Path | None, slug: str, name: str, content: str) -> None:
    if debug_dir is None:
        return
    d = Path(debug_dir) / slug
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_text(content, encoding="utf-8")


# ------------------------------------------------------------------ single page
def write_page(conn: sqlite3.Connection, spec: PageSpec, *, chat_fn, model: str,
               out_dir: Path, force: bool = False, git_head: str = "", since_ref: str = "",
               budget: int | None = None,
               debug_dir: Path | None = None) -> tuple[WriteResult, dict | None]:
    """Returns (result, manifest_entry). manifest_entry is None only when nothing exists."""
    packages = render._packages_for(conn, spec.include)
    if not packages:
        return WriteResult(slug=spec.slug, status="skipped"), None

    phash = page_writer_hash(conn, spec, packages, model)
    prev = db.get_page_build(conn, spec.slug)
    if prev and prev["hash"] == phash and not force and (out_dir / f"{spec.slug}.md").exists():
        return (WriteResult(slug=spec.slug, status="skipped_fresh"),
                json.loads(prev["meta_json"] or "{}") or None)

    since = since_ref or (prev or {}).get("git_head") or ""
    evidence = gitlog.evidence_block(gitlog.changed_since(since), spec.include) if since else ""
    b = B.build_bundle(conn, spec, packages, git_evidence=evidence)
    if budget is None:
        budget = context_budget(model)
    result = WriteResult(slug=spec.slug, status="written")
    if budget < MIN_BUDGET:
        result.errors.append(f"context budget {budget} too small")
        return _fallback(conn, spec, out_dir, result)
    b = B.trim_to_budget(b, budget)

    # ---- stage 1: plan (transport failure here ⇒ the LLM is unreachable ⇒ Jinja)
    try:
        skel, plan_usage = skeleton.plan_page(conn, spec, b, chat_fn=chat_fn, model=model,
                                              max_tokens=PLANNER_MAX_TOKENS)
    except Exception as exc:
        result.errors.append(f"planner LLM call failed: {exc}")
        return _fallback(conn, spec, out_dir, result)
    result.tokens_in += plan_usage.get("prompt_tokens", 0)
    result.tokens_out += plan_usage.get("completion_tokens", 0)
    _dump_debug(debug_dir, spec.slug, "skeleton.json",
                json.dumps(skel.to_dict(), indent=2, ensure_ascii=False))

    # ---- stage 2: fill sections consecutively (keeps the prompt-prefix cache hot)
    shared_prefix = sections.build_shared_prefix(spec, skel, b)
    _dump_debug(debug_dir, spec.slug, "shared_prefix.txt", shared_prefix)
    sections_md: list[tuple[skeleton.SectionPlan, str]] = []
    section_stats: dict[str, dict] = {}
    n_fallback = 0
    for sec in skel.sections:
        md, usage, info = sections.fill_section(conn, sec, skel, shared_prefix, b,
                                                chat_fn=chat_fn, model=model,
                                                max_tokens=SECTION_MAX_TOKENS)
        result.tokens_in += usage.get("prompt_tokens", 0)
        result.tokens_out += usage.get("completion_tokens", 0)
        result.warnings += [f"{sec.id}: {w}" for w in info["warnings"]]
        if info["fallback"]:
            n_fallback += 1
            result.errors += [f"{sec.id}: {e}" for e in info["errors"]]
        section_stats[sec.id] = {k: info[k] for k in ("errors", "warnings", "fallback", "retried")}
        sections_md.append((sec, md))
        _dump_debug(debug_dir, spec.slug, f"{sec.id}.md", md)

    # ---- stage 3: diagrams (architecture slot is always the deterministic diagram)
    diagrams_filled: dict[str, str] = {}
    for sec in skel.sections:
        if not sec.diagram:
            continue
        if sec.diagram.get("type") == "architecture":
            if b.det_diagram:
                diagrams_filled[sec.id] = b.det_diagram
            continue
        block, usage = D.fill_diagram(sec, b, conn, chat_fn=chat_fn, model=model,
                                      max_tokens=DIAGRAM_MAX_TOKENS)
        result.tokens_in += (usage or {}).get("prompt_tokens", 0)
        result.tokens_out += (usage or {}).get("completion_tokens", 0)
        if block:
            diagrams_filled[sec.id] = block
        else:
            result.warnings.append(f"{sec.id}: planned diagram failed validation — dropped")

    # ---- stage 4: assemble + page-level checks + Sources linkification
    page_md, page_errors, page_warnings = sections.assemble_page(conn, spec.title, sections_md,
                                                                 diagrams_filled)
    result.warnings += page_warnings
    result.errors += page_errors

    degraded = (n_fallback > len(skel.sections) * MAX_FALLBACK_RATIO) or bool(page_errors)
    result.fresh = not degraded

    entry = _manifest_entry(spec, page_md, b)
    (out_dir / f"{spec.slug}.md").write_text(page_md, encoding="utf-8")
    if not degraded:
        db.upsert_page_build(
            conn, slug=spec.slug, hash_=phash, git_head=git_head, model=model,
            status="written", tokens_in=result.tokens_in, tokens_out=result.tokens_out,
            validator={"planner": skel.source, "sections": section_stats,
                       "n_fallback_sections": n_fallback,
                       "page_warnings": page_warnings},
            meta=entry, written_at=datetime.now(timezone.utc).isoformat(),
            skeleton=skel.to_dict())
    return result, entry


def _fallback(conn: sqlite3.Connection, spec: PageSpec, out_dir: Path,
              result: WriteResult) -> tuple[WriteResult, dict | None]:
    """Deterministic Jinja page; NO page_builds hash stored → retried next run."""
    result.status = "fallback_jinja"
    result.fresh = False
    page = render.build_page(conn, spec, chat_fn=None, model="")
    if page is None:
        return result, None
    md = page.pop("_markdown")
    (out_dir / page["file"]).write_text(md, encoding="utf-8")
    return result, page


def _manifest_entry(spec: PageSpec, md: str, b: B.PageBundle) -> dict:
    # summary = first prose paragraph after the title
    summary = ""
    for para in md.split("\n\n")[1:]:
        p = para.strip()
        if p and not p.startswith(("#", "```", "|", "-", "*")):
            summary = render._first_sentence(p)
            break
    refs = sorted({s["file_path"] for s in b.key_symbols}
                  | {path for path, _, _ in b.module_summaries})[:render.MAX_SOURCE_REFS]
    return {"id": f"{spec.order:02d}-{spec.slug}", "slug": spec.slug, "title": spec.title,
            "order": spec.order, "summary": summary, "file": f"{spec.slug}.md",
            "source_refs": refs,
            "written_at": datetime.now(timezone.utc).isoformat()}


# ------------------------------------------------------------------ quickstart
def write_quickstart(conn: sqlite3.Connection, pages_meta: list[dict], *, chat_fn, model: str,
                     out_dir: Path, git_head: str = "", force: bool = False) -> dict | None:
    if not pages_meta:
        return None
    qhash_src = "|".join(sorted(f"{p['slug']}:{p.get('summary', '')}" for p in pages_meta))
    qhash = hashlib.sha256(
        f"{qhash_src}|{prompts.WRITER_PROMPT_VERSION}|{model}".encode("utf-8")).hexdigest()
    prev = db.get_page_build(conn, "quickstart")
    if prev and prev["hash"] == qhash and not force and (out_dir / "quickstart.md").exists():
        return json.loads(prev["meta_json"] or "{}") or None

    intro = ""
    if chat_fn is not None:
        ctx_block = "\n".join(f"- {p['title']}: {p.get('summary', '')}" for p in pages_meta)
        try:
            text, _ = chat_fn(prompts.quickstart_user(ctx_block), model=model,
                              system=prompts.QUICKSTART_SYSTEM,
                              max_tokens=QUICKSTART_MAX_TOKENS, timeout=CALL_TIMEOUT)
            intro = validate._THINK_RE.sub("", text).strip()
        except Exception:
            intro = ""
    if not intro:
        from codewiki.paths import REPO_ROOT
        intro = (f"This wiki is generated from the code graph of the {REPO_ROOT.name} "
                 "repository. Every page carries verified Sources links and diagrams computed "
                 "from source, not written from memory.")

    quickstart_written_at = datetime.now(timezone.utc).isoformat()
    rows = "\n".join(f"| [{p['title']}]({p['file']}) | {p.get('summary', '')} |"
                     for p in sorted(pages_meta, key=lambda p: p["order"]))
    md = (f"# Quickstart\n\n{intro}\n\n"
          "## Pages\n\n| Page | What it covers |\n|---|---|\n" + rows + "\n\n"
          "## How this wiki is maintained\n\n"
          "Generated by `codewiki` (tree-sitter code graph → hash-gated local-LLM summaries → "
          "a plan-then-fill page writer with per-section Sources validation). Regenerate after "
          "structural changes with `codewiki update`; only pages whose underlying code changed "
          "are rewritten.\n")
    (out_dir / "quickstart.md").write_text(md, encoding="utf-8")
    entry = {"id": "00-quickstart", "slug": "quickstart", "title": "Quickstart", "order": 0,
             "summary": render._first_sentence(intro), "file": "quickstart.md",
             "source_refs": [], "written_at": quickstart_written_at}
    db.upsert_page_build(conn, slug="quickstart", hash_=qhash, git_head=git_head, model=model,
                         status="written", meta=entry,
                         written_at=quickstart_written_at)
    return entry


# ------------------------------------------------------------------ full assemble
def assemble_writer(conn: sqlite3.Connection, *, chat_fn, model: str, force: bool = False,
                    only: set[str] | None = None, out_dir: Path | None = None,
                    prune: bool = False, since_ref: str = "",
                    on_progress: Callable[[int, int], None] | None = None,
                    debug_dir: Path | None = None, verbose: bool = True) -> dict:
    """Write all (stale) pages + quickstart + manifest. Returns the manifest."""
    production = out_dir is None
    out = Path(out_dir) if out_dir else WIKI_DIR
    out.mkdir(parents=True, exist_ok=True)
    git_head = db.get_meta(conn, "head", "") or ""

    specs = load_pages()
    n_total = len(specs) + 1                                # +1 = quickstart
    pages_meta: list[dict] = []
    changed = 0
    for i, spec in enumerate(specs):
        if only and spec.slug not in only:
            prev = db.get_page_build(conn, spec.slug)
            meta = json.loads(prev["meta_json"] or "{}") if prev else {}
            if meta:
                pages_meta.append(meta)
            continue
        result, entry = write_page(conn, spec, chat_fn=chat_fn, model=model, out_dir=out,
                                   force=force, git_head=git_head, since_ref=since_ref,
                                   debug_dir=debug_dir)
        if verbose:
            note = f" ({'; '.join(result.errors)})" if result.errors else ""
            stale = "" if result.fresh else " [stays stale — retried next run]"
            print(f"  {spec.slug}: {result.status} in={result.tokens_in} "
                  f"out={result.tokens_out}{stale}{note}")
        if entry:
            pages_meta.append(entry)
        if result.status in ("written", "fallback_jinja"):
            changed += 1
        conn.commit()                                   # crash-safe: keep finished pages
        if on_progress:
            on_progress(i + 1, n_total)

    q_entry = write_quickstart(conn, pages_meta, chat_fn=chat_fn, model=model, out_dir=out,
                               git_head=git_head, force=force)
    if q_entry:
        pages_meta.insert(0, q_entry)
    if on_progress:
        on_progress(n_total, n_total)

    # entries cached before written_at existed: backfill from page_builds (one-time rewrite)
    migrated = False
    for p in pages_meta:
        if not p.get("written_at"):
            build = db.get_page_build(conn, p["slug"])
            if build and build.get("written_at"):
                p["written_at"] = build["written_at"]
                migrated = True

    manifest = {"generated_at": datetime.now(timezone.utc).isoformat(),
                "model": model or "codewiki-deterministic",
                "page_count": len(pages_meta),
                "pages": sorted(pages_meta, key=lambda p: p["order"])}
    manifest_path = out / "manifest.json" if out_dir else PAGE_MANIFEST
    if changed or force or migrated or not manifest_path.exists():
        manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n",
                                 encoding="utf-8")
        db.set_meta(conn, "pages_head", git_head)
    elif verbose:
        print(f"  wiki already current ({len(pages_meta)} pages fresh) — manifest untouched")

    if prune:
        keep = {p["file"] for p in pages_meta} | {"manifest.json"}
        for f in out.glob("*.md"):
            if f.name not in keep:
                f.unlink()
                if verbose:
                    print(f"  pruned {f.name}")

    if production and changed:
        if ensure_pointer_section() and verbose:
            print("  refreshed the CLAUDE.md wiki pointer section")
    conn.commit()
    return manifest
