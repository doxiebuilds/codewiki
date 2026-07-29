"""
render.py — assemble wiki pages from the code graph + stored summaries (deterministic).

For each page in pages.yaml we gather its packages' summaries, notable modules and symbols (with
real ``path:line`` citations straight from the graph), the requested domain-node reference tables,
and a graph-derived Mermaid diagram, then render through the Jinja skeleton. Output is the SAME
contract the old pipeline emitted — ``docs/wiki/<slug>.md`` + ``manifest.json`` — so the FastAPI
routes, Help Center tab and search are untouched.

The page *overview* prose is the one optional LLM touch (a summary-of-package-summaries), and it
is hash-gated like every other node: pass ``chat_fn=None`` to render a fully deterministic page
from already-stored summaries (used by the output-parity test).
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import PurePosixPath

from jinja2 import Environment, FileSystemLoader, select_autoescape

from codewiki.assembly import diagrams
from codewiki.assembly.pages import PageSpec, load_pages
from codewiki.generator import summarize as S
from codewiki.paths import PAGE_MANIFEST, WIKI_DIR
from codewiki.store import db

_TEMPLATES = PurePosixPath(__file__).parent.parent / "templates"
_env = Environment(loader=FileSystemLoader(str(_TEMPLATES)), autoescape=select_autoescape([]),
                   trim_blocks=False, lstrip_blocks=False, keep_trailing_newline=True)

MAX_PKGS = 8
MAX_MODULES = 6
MAX_SYMBOLS = 8
MAX_SOURCE_REFS = 15


# ------------------------------------------------------------------ small helpers
def _md_cell(text: str, limit: int = 180) -> str:
    return " ".join((text or "").split()).replace("|", "\\|")[:limit]


def _summary_text(summary_json: str | None) -> str:
    if not summary_json:
        return ""
    try:
        d = json.loads(summary_json)
    except json.JSONDecodeError:
        return ""
    return d.get("summary") or d.get("purpose") or ""


def _first_sentence(text: str, limit: int = 200) -> str:
    text = " ".join(text.split())
    cut = text.find(". ")
    s = text[: cut + 1] if cut != -1 else text
    return (s[:limit] + "…") if len(s) > limit else s


# ------------------------------------------------------------------ graph queries
def _packages_for(conn: sqlite3.Connection, prefixes: list[str]) -> list[str]:
    all_pkgs = [r["package"] for r in conn.execute(
        "SELECT package, COUNT(*) n FROM symbols GROUP BY package ORDER BY n DESC")]
    out = []
    for pkg in all_pkgs:
        if any(pkg == p or pkg.startswith(p + "/") or pkg == p for p in prefixes):
            out.append(pkg)
    return out


def _pkg_summary(conn: sqlite3.Connection, pkg: str) -> str:
    row = conn.execute("SELECT summary_json FROM summaries WHERE node_id=?", (f"pkg::{pkg}",)).fetchone()
    return _summary_text(row["summary_json"]) if row else ""


def _modules(conn: sqlite3.Connection, pkg: str, limit: int) -> list[dict]:
    rows = conn.execute(
        "SELECT s.file_path, su.summary_json FROM symbols s "
        "LEFT JOIN summaries su ON su.node_id=s.id "
        "JOIN files f ON f.path=s.file_path "
        "WHERE s.kind='module' AND s.qualname='' AND s.package=? "
        "ORDER BY f.n_symbols DESC LIMIT ?", (pkg, limit))
    out = []
    for r in rows:
        summ = _summary_text(r["summary_json"]) or "(no summary yet)"
        out.append({"path": r["file_path"], "summary": _md_cell(summ, 200)})
    return out


def _key_symbols(conn: sqlite3.Connection, pkg: str, limit: int) -> list[dict]:
    rows = conn.execute(
        "SELECT s.name, s.kind, s.qualname, s.signature, s.file_path, s.start_line, su.summary_json "
        "FROM symbols s JOIN summaries su ON su.node_id=s.id "
        "WHERE s.package=? AND s.kind IN ('class','function') "
        "ORDER BY CASE s.kind WHEN 'class' THEN 0 ELSE 1 END, s.start_line LIMIT ?", (pkg, limit))
    out = []
    for r in rows:
        loc = f"{r['file_path']}:{r['start_line']}"
        out.append({"sig": _md_cell(r["signature"] or r["name"], 120), "loc": loc,
                    "path": r["file_path"], "line": r["start_line"],
                    "summary": _md_cell(_summary_text(r["summary_json"]), 160)})
    return out


def _domain_table(conn: sqlite3.Connection, kind: str, prefixes: list[str]) -> dict | None:
    rows = conn.execute(
        "SELECT name, detail, file_path, line FROM domain_nodes WHERE kind=? ORDER BY name", (kind,)
    ).fetchall()
    if kind in {"route", "ws_event", "env_flag", "api_call"}:
        rows = [r for r in rows if r["file_path"] and any(
            r["file_path"].startswith(p) for p in prefixes)]
    if not rows:
        return None

    if kind == "service":
        header = "| Service | Tier | Description |\n|---|---|---|"
        body = []
        for r in sorted(rows, key=lambda r: (json.loads(r["detail"]).get("tier") or "", r["name"])):
            d = json.loads(r["detail"])
            body.append(f"| `{_md_cell(r['name'])}` | {d.get('tier','')} | {_md_cell(d.get('description',''))} |")
        return {"title": "Services & Tiers", "markdown": header + "\n" + "\n".join(body)}

    if kind == "route":
        header = "| Method | Path | Handler | Source |\n|---|---|---|---|"
        body = []
        for r in rows:
            d = json.loads(r["detail"])
            body.append(f"| {d.get('method','')} | `{_md_cell(r['name'])}` | `{d.get('func','')}` | `{r['file_path']}:{r['line']}` |")
        return {"title": "HTTP Routes", "markdown": header + "\n" + "\n".join(body)}

    if kind == "db_table":
        header = "| Table | Defined in |\n|---|---|"
        body = [f"| `{_md_cell(r['name'])}` | `{r['file_path']}:{r['line']}` |" for r in rows]
        return {"title": "Database Tables", "markdown": header + "\n" + "\n".join(body)}

    if kind == "redis_channel":
        header = "| Channel | Publish sites | Subscribe sites | First publisher | First subscriber |\n|---|---|---|---|---|"
        body = []
        for r in rows:
            d = json.loads(r["detail"] or "{}")
            pub = f"`{d['publish']}`" if d.get("publish") else "—"
            sub = f"`{d['subscribe']}`" if d.get("subscribe") else "—"
            body.append(f"| `{_md_cell(r['name'])}` | {d.get('n_publish', '')} | "
                        f"{d.get('n_subscribe', '')} | {pub} | {sub} |")
        return {"title": "Redis Channels", "markdown": header + "\n" + "\n".join(body)}

    if kind == "ws_event":
        header = "| Event type | Source |\n|---|---|"
        body = [f"| `{_md_cell(r['name'])}` | `{r['file_path']}:{r['line']}` |" for r in rows]
        return {"title": "WebSocket Events", "markdown": header + "\n" + "\n".join(body)}

    if kind == "ffi_export":
        header = "| Python-visible export | Kind | Signature | Source |\n|---|---|---|---|"
        body = []
        for r in rows:
            d = json.loads(r["detail"] or "{}")
            body.append(f"| `{_md_cell(r['name'])}` | {d.get('kind','')} | "
                        f"`{_md_cell(d.get('signature',''), 100)}` | `{r['file_path']}:{r['line']}` |")
        return {"title": "Rust → Python FFI Exports", "markdown": header + "\n" + "\n".join(body)}

    if kind == "api_call":
        header = "| Frontend call | Matched route | Sites | Source |\n|---|---|---|---|"
        body = []
        for r in rows:
            d = json.loads(r["detail"] or "{}")
            matched = f"`{_md_cell(d['route'])}`" if d.get("route") else "—"
            body.append(f"| `{_md_cell(r['name'])}` | {matched} | {d.get('n_sites','')} | "
                        f"`{r['file_path']}:{r['line']}` |")
        return {"title": "Frontend API Calls", "markdown": header + "\n" + "\n".join(body)}

    if kind == "env_flag":
        header = "| Env var | Default | Read sites | First seen |\n|---|---|---|---|"
        body = []
        for r in rows:
            d = json.loads(r["detail"] or "{}")
            default = f"`{_md_cell(d['default'], 40)}`" if d.get("default") else "—"
            body.append(f"| `{_md_cell(r['name'])}` | {default} | {d.get('n_sites','')} | "
                        f"`{r['file_path']}:{r['line']}` |")
        return {"title": "Environment Flags", "markdown": header + "\n" + "\n".join(body)}
    return None


# ------------------------------------------------------------------ page build
def _page_hash(conn: sqlite3.Connection, spec: PageSpec, packages: list[str]) -> str:
    parts = [S.package_rollup_hash(conn, p) for p in packages]
    parts.append("domain:" + ",".join(sorted(spec.domain)))
    return hashlib.sha256("|".join(sorted(parts)).encode("utf-8")).hexdigest()


def _overview(conn: sqlite3.Connection, spec: PageSpec, packages: list[str], page_hash: str,
              chat_fn, model: str) -> str:
    node_id = f"page::{spec.slug}"
    pkg_lines = []
    for p in packages[:12]:
        summ = _pkg_summary(conn, p)
        if summ:
            pkg_lines.append(f"  - {diagrams._short(p)}: {summ}")
    block = f"PAGE: {spec.title}\nPACKAGES:\n" + "\n".join(pkg_lines)

    if chat_fn is not None and pkg_lines:
        if db.summary_hash(conn, node_id) != page_hash:
            try:
                text, usage = chat_fn(S._container_prompt("module", block), model=model)
                summary = S._parse_json(text)
                db.upsert_summary(conn, node_id=node_id, node_kind="page", hash_=page_hash,
                                  summary=summary, model=model,
                                  tokens_in=usage.get("prompt_tokens", 0),
                                  tokens_out=usage.get("completion_tokens", 0),
                                  generated_at=datetime.now(timezone.utc).isoformat())
            except Exception as exc:  # fall back to deterministic overview
                print(f"  page overview LLM failed for {spec.slug}: {exc}")
        row = db.get_summary(conn, node_id)
        if row:
            return _summary_text(row["summary_json"])

    # deterministic fallback: stitch the top package summaries
    stored = db.get_summary(conn, node_id)
    if stored:
        return _summary_text(stored["summary_json"])
    lead = f"{spec.title} spans {len(packages)} package(s)."
    joined = " ".join(s for s in (_pkg_summary(conn, p) for p in packages[:3]) if s)
    return (lead + " " + joined).strip()


def build_page(conn: sqlite3.Connection, spec: PageSpec, *, chat_fn, model: str) -> dict | None:
    packages = _packages_for(conn, spec.include)
    if not packages:
        return None
    page_hash = _page_hash(conn, spec, packages)
    overview = _overview(conn, spec, packages, page_hash, chat_fn, model)

    sections = []
    source_refs: list[str] = []
    for pkg in packages[:MAX_PKGS]:
        modules = _modules(conn, pkg, MAX_MODULES)
        source_refs.extend(m["path"] for m in modules)
        sections.append({
            "heading": diagrams._short(pkg),
            "body": _pkg_summary(conn, pkg) or "_No package summary yet._",
            "modules": modules,
            "symbols": _key_symbols(conn, pkg, MAX_SYMBOLS),
        })

    tables = [t for t in (_domain_table(conn, k, spec.include) for k in spec.domain) if t]
    diagram = diagrams.package_dependency_mermaid(conn, set(packages))

    md = _env.get_template("page.md.j2").render(
        title=spec.title, overview=overview or f"# {spec.title}", diagram=diagram,
        sections=sections, tables=tables,
        generated_at=datetime.now(timezone.utc).strftime("%Y-%m-%d"),
    )
    return {
        "id": f"{spec.order:02d}-{spec.slug}", "slug": spec.slug, "title": spec.title,
        "order": spec.order, "summary": _first_sentence(overview),
        "file": f"{spec.slug}.md", "source_refs": sorted(set(source_refs))[:MAX_SOURCE_REFS],
        "written_at": datetime.now(timezone.utc).isoformat(),
        "_markdown": md,
    }


def assemble(conn: sqlite3.Connection, *, chat_fn=None, model: str = "", write: bool = True) -> dict:
    """Render all pages and (optionally) write docs/wiki/*.md + manifest.json. Returns manifest."""
    specs = load_pages()
    pages_meta = []
    WIKI_DIR.mkdir(parents=True, exist_ok=True)
    for spec in specs:
        page = build_page(conn, spec, chat_fn=chat_fn, model=model)
        if page is None:
            print(f"  skip {spec.slug}: no packages matched {spec.include}")
            continue
        markdown = page.pop("_markdown")
        if write:
            (WIKI_DIR / page["file"]).write_text(markdown, encoding="utf-8")
        pages_meta.append(page)

    manifest = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "model": model or "codewiki-deterministic",
        "page_count": len(pages_meta),
        "pages": pages_meta,
    }
    if write:
        PAGE_MANIFEST.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n",
                                 encoding="utf-8")
    conn.commit()
    return manifest
