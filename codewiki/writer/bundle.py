"""
bundle.py — the per-page context bundle for the LLM page writer (deterministic; no LLM).

The analog of generator/context.py one level up: instead of letting the model roam, we hand it
everything a high-quality narrative page needs — package/module summaries, key symbols with
exact locations, real source excerpts, cross-package relationships (including the resolve.py
call edges and publishes/consumes channel edges), the domain reference tables, the deterministic
architecture diagram, and git evidence since the page's last build.

Every unit of context is also an addressable **EvidenceItem** (E1..En). The planner sees only
the one-line labels (``evidence_catalog``); each section writer sees only its sections' full
blocks (``evidence_slice_text``) and may only cite that slice's locations (``allowed_cites``).
Test files are de-prioritized (ranked last, capped) on pages that did not opt in via
``PageSpec.keep_tests``, and trivial ``__init__.py`` modules never make the cut.

``trim_to_budget`` drops whole low-rank EvidenceItems (excerpt tails → module tails →
symbol/edges tails → table-row truncation) so surviving eids stay stable; the legacy parallel
fields are rebuilt as views of the surviving items. Package summaries, the diagram and git
evidence are never trimmed.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field, replace
from pathlib import PurePosixPath

from codewiki import config as C
from codewiki.assembly import diagrams, render
from codewiki.assembly.pages import PageSpec
from codewiki.generator import context as ctx
from codewiki.paths import is_test_path

MAX_PKGS = 12
MAX_MODULES = 36
MAX_SYMBOLS = 30
MAX_EXCERPTS = 6
MAX_EXCERPT_LINES = 80
MAX_EDGES = 40
MAX_CITATIONS = 150
MAX_LABEL = 140
EDGE_GROUP_SIZE = 8            # edge lines per `edges` EvidenceItem


def _table_cite_re() -> re.Pattern:
    prefix = C.root_prefix()
    if prefix:
        p = re.escape(prefix.rstrip("/"))
        return re.compile(rf"`({p}/[\w./\-]+):(\d+)(?:-(\d+))?`")
    return re.compile(r"`([\w][\w./\-]*\.(?:py|rs|ts|tsx|js|jsx|sql|ya?ml|toml|json|sh)):(\d+)(?:-(\d+))?`")


@dataclass
class EvidenceItem:
    """One addressable unit of verified page evidence (planner label + writer block)."""
    eid: str                                   # "E1", "E2", … stable for the page build
    kind: str                                  # pkg|module|symbol|excerpt|edges|table|git
    label: str                                 # <=140-char one-liner (planner-facing)
    text: str                                  # full block (section-writer-facing)
    cites: list[str] = field(default_factory=list)   # legal "path:a-b" locations
    data: object = None                        # backing object for legacy-view rebuilds


@dataclass
class PageBundle:
    spec: PageSpec
    packages: list[str]
    pkg_summaries: list[tuple[str, str]] = field(default_factory=list)      # (short_pkg, text)
    module_summaries: list[tuple[str, str, int]] = field(default_factory=list)  # (path, text, n_lines)
    key_symbols: list[dict] = field(default_factory=list)
    excerpts: list[dict] = field(default_factory=list)                      # {loc, source}
    edges_digest: list[str] = field(default_factory=list)
    domain_tables: list[dict] = field(default_factory=list)                 # render._domain_table
    det_diagram: str = ""
    git_evidence: str = ""
    citation_index: list[str] = field(default_factory=list)
    evidence: list[EvidenceItem] = field(default_factory=list)


def estimate_tokens(text: str) -> int:
    return len(text) // 4


def _one_line(text: str, limit: int = MAX_LABEL) -> str:
    flat = " ".join((text or "").split())
    return flat[: limit - 1] + "…" if len(flat) > limit else flat


def _is_trivial_module(row) -> bool:
    """`__init__.py` package markers with (nearly) nothing in them — noise, not evidence."""
    return (PurePosixPath(row["file_path"]).name == "__init__.py"
            and (row["n_symbols"] <= 1 or row["end_line"] < 3))


def _module_rows(conn: sqlite3.Connection, pkg: str, limit: int, *,
                 keep_tests: bool) -> list[sqlite3.Row]:
    rows = conn.execute(
        "SELECT s.file_path, s.end_line, f.n_symbols, su.summary_json FROM symbols s "
        "LEFT JOIN summaries su ON su.node_id=s.id JOIN files f ON f.path=s.file_path "
        "WHERE s.kind='module' AND s.qualname='' AND s.package=? "
        "ORDER BY f.n_symbols DESC LIMIT ?", (pkg, limit * 3)).fetchall()
    rows = [r for r in rows if not _is_trivial_module(r)]
    # rank test files last on non-test pages (stable within each group: by symbol count desc)
    rows.sort(key=lambda r: (is_test_path(r["file_path"]) and not keep_tests, -r["n_symbols"]))
    if keep_tests:
        return rows[:limit]
    test_cap = max(1, limit // 5)                       # ~20% of the module slots
    out: list[sqlite3.Row] = []
    n_test = 0
    for r in rows:
        if is_test_path(r["file_path"]):
            if n_test >= test_cap:
                continue
            n_test += 1
        out.append(r)
        if len(out) >= limit:
            break
    return out


def _key_symbol_rows(conn: sqlite3.Connection, packages: list[str], limit: int, *,
                     keep_tests: bool) -> list[sqlite3.Row]:
    qmarks = ",".join("?" * len(packages))
    rows = conn.execute(
        f"SELECT s.name, s.kind, s.qualname, s.signature, s.file_path, s.start_line, s.end_line, "
        f"su.summary_json FROM symbols s JOIN summaries su ON su.node_id=s.id "
        f"WHERE s.package IN ({qmarks}) AND s.kind IN ('class','function') "
        f"ORDER BY (s.end_line - s.start_line) DESC LIMIT ?", (*packages, limit * 3)).fetchall()
    # stable sort: test symbols sink to the bottom on non-test pages, size order kept within groups
    rows.sort(key=lambda r: is_test_path(r["file_path"]) and not keep_tests)
    if keep_tests:
        return rows[:limit]
    test_cap = max(1, (limit * 4) // 30)                # cap 4/30 test symbols
    out: list[sqlite3.Row] = []
    n_test = 0
    for r in rows:
        if is_test_path(r["file_path"]):
            if n_test >= test_cap:
                continue
            n_test += 1
        out.append(r)
        if len(out) >= limit:
            break
    return out


def _edges_digest(conn: sqlite3.Connection, packages: list[str],
                  limit: int) -> list[tuple[str, str]]:
    """Cross-package resolved calls + channel flows as (dst_group, line) pairs."""
    qmarks = ",".join("?" * len(packages))
    out: list[tuple[str, str]] = []
    for r in conn.execute(
            f"SELECT DISTINCT src.qualname sq, src.package sp, dst.qualname dq, dst.package dp "
            f"FROM edges e JOIN symbols src ON src.id=e.src_id JOIN symbols dst ON dst.id=e.dst_id "
            f"WHERE e.kind='calls' AND e.resolved IN ('import','unique') "
            f"AND src.package IN ({qmarks}) AND src.package != dst.package "
            f"ORDER BY sp, sq LIMIT ?", (*packages, limit)):
        out.append((diagrams._short(r["dp"]),
                    f"{diagrams._short(r['sp'])}:{r['sq'] or '<module>'} -> "
                    f"{diagrams._short(r['dp'])}:{r['dq'] or '<module>'} (calls)"))
    remaining = limit - len(out)
    if remaining > 0:
        for r in conn.execute(
                f"SELECT DISTINCT s.qualname sq, s.package sp, e.kind k, e.dst_name chan "
                f"FROM edges e JOIN symbols s ON s.id=e.src_id "
                f"WHERE e.kind IN ('publishes','consumes') AND s.package IN ({qmarks}) "
                f"ORDER BY e.dst_name LIMIT ?", (*packages, remaining)):
            arrow = "->" if r["k"] == "publishes" else "<-"
            out.append(("redis",
                        f"{diagrams._short(r['sp'])}:{r['sq'] or '<module>'} {arrow} "
                        f"redis[{r['chan']}] ({r['k']})"))
    return out


# ------------------------------------------------------------------ evidence construction
def _build_evidence(b: PageBundle, edge_pairs: list[tuple[str, str]],
                    keep_tests: bool) -> list[EvidenceItem]:
    items: list[EvidenceItem] = []

    def add(kind: str, label: str, text: str, cites: list[str], data) -> None:
        items.append(EvidenceItem(eid=f"E{len(items) + 1}", kind=kind,
                                  label=_one_line(label), text=text, cites=cites, data=data))

    for short_pkg, summ in b.pkg_summaries:
        add("pkg", f"pkg {short_pkg}: {summ}", f"PACKAGE {short_pkg}: {summ}", [], (short_pkg, summ))

    for path, summ, n_lines in b.module_summaries:
        name = PurePosixPath(path).name
        prefix = "[module:test] " if is_test_path(path) and not keep_tests else ""
        add("module", f"{prefix}module {name}: {summ}",
            f"MODULE {path} [1-{n_lines}]: {summ}",
            [f"{path}:1-{n_lines}"], (path, summ, n_lines))

    for s in b.key_symbols:
        loc = f"{s['file_path']}:{s['start_line']}-{s['end_line']}"
        add("symbol", f"symbol {s['kind']} {s['qualname']}: {s['summary']}",
            f"{s['kind']} {s['qualname']} — {loc} — {s['signature']} — {s['summary']}",
            [loc], s)

    for e in b.excerpts:
        add("excerpt", f"excerpt {e['loc']}",
            f"SOURCE EXCERPT {e['loc']}\n```\n{e['source']}\n```", [e["loc"]], e)

    # edge groups of <= EDGE_GROUP_SIZE lines, grouped by destination package
    groups: dict[str, list[str]] = {}
    order: list[str] = []
    for group, line in edge_pairs:
        if group not in groups:
            groups[group] = []
            order.append(group)
        groups[group].append(line)
    for group in order:
        lines = groups[group]
        for i in range(0, len(lines), EDGE_GROUP_SIZE):
            chunk = lines[i:i + EDGE_GROUP_SIZE]
            add("edges", f"edges → {group}: {len(chunk)} verified call/channel edges",
                "\n".join(chunk), [], list(chunk))

    for t in b.domain_tables:
        cites = []
        for m in _table_cite_re().finditer(t["markdown"]):
            rng = f"{m.group(1)}:{m.group(2)}" + (f"-{m.group(3)}" if m.group(3) else "")
            cites.append(rng)
        n_rows = max(0, len(t["markdown"].splitlines()) - 2)
        add("table", f"table {t['title']}: {n_rows} rows",
            f"### {t['title']}\n{t['markdown']}", list(dict.fromkeys(cites)), t)

    if b.git_evidence:
        add("git", "git: recent changes since this page's last build", b.git_evidence, [],
            b.git_evidence)
    return items


def evidence_catalog(b: PageBundle) -> str:
    """Planner-facing catalog: labels only, one per line."""
    return "\n".join(f"{it.eid} [{it.kind}] {it.label}" for it in b.evidence)


def evidence_slice_text(b: PageBundle, eids: list[str]) -> str:
    """Full blocks of the selected items, in bundle order."""
    want = set(eids)
    parts: list[str] = []
    for it in b.evidence:
        if it.eid not in want:
            continue
        block = f"[{it.eid} — {it.kind}]\n{it.text}"
        if it.cites:
            block += "\nLOCATIONS: " + ", ".join(it.cites)
        parts.append(block)
    return "\n\n".join(parts)


def allowed_cites(b: PageBundle, eids: list[str]) -> set[str]:
    """The only locations a section holding these eids may put in its Sources block."""
    want = set(eids)
    out: set[str] = set()
    for it in b.evidence:
        if it.eid in want:
            out.update(it.cites)
    return out


# ------------------------------------------------------------------ bundle build
def build_bundle(conn: sqlite3.Connection, spec: PageSpec, packages: list[str], *,
                 git_evidence: str = "") -> PageBundle:
    b = PageBundle(spec=spec, packages=packages[:MAX_PKGS])
    keep_tests = spec.keep_tests

    for pkg in b.packages:
        summ = render._pkg_summary(conn, pkg)
        if summ:
            b.pkg_summaries.append((diagrams._short(pkg), summ))

    per_pkg = max(2, MAX_MODULES // max(1, len(b.packages)))
    seen_paths: set[str] = set()
    for pkg in b.packages:
        for r in _module_rows(conn, pkg, per_pkg, keep_tests=keep_tests):
            if r["file_path"] in seen_paths:
                continue
            seen_paths.add(r["file_path"])
            text = render._summary_text(r["summary_json"]) or "(no summary yet)"
            b.module_summaries.append((r["file_path"], text, r["end_line"]))
    b.module_summaries = b.module_summaries[:MAX_MODULES]

    for r in _key_symbol_rows(conn, b.packages, MAX_SYMBOLS, keep_tests=keep_tests):
        b.key_symbols.append({
            "kind": r["kind"], "qualname": r["qualname"] or r["name"],
            "signature": r["signature"] or r["name"], "file_path": r["file_path"],
            "start_line": r["start_line"], "end_line": r["end_line"],
            "summary": render._summary_text(r["summary_json"]),
        })

    # source excerpts: the largest key symbols (they anchor the narrative)
    for s in b.key_symbols[:MAX_EXCERPTS]:
        end = min(s["end_line"], s["start_line"] + MAX_EXCERPT_LINES - 1)
        src = ctx._read_span(s["file_path"], s["start_line"], end)
        if src:
            b.excerpts.append({"loc": f"{s['file_path']}:{s['start_line']}-{end}", "source": src})

    edge_pairs = _edges_digest(conn, b.packages, MAX_EDGES)
    b.edges_digest = [line for _, line in edge_pairs]
    b.domain_tables = [t for t in (render._domain_table(conn, k, spec.include)
                                   for k in spec.domain) if t]
    b.det_diagram = diagrams.package_dependency_mermaid(conn, set(b.packages),
                                                        include_tests=keep_tests)
    b.git_evidence = git_evidence

    cites: list[str] = []
    for path, _, n_lines in b.module_summaries:
        cites.append(f"{path}:1-{n_lines}")
    for s in b.key_symbols:
        cites.append(f"{s['file_path']}:{s['start_line']}-{s['end_line']}")
    b.citation_index = list(dict.fromkeys(cites))[:MAX_CITATIONS]

    b.evidence = _build_evidence(b, edge_pairs, keep_tests)
    return b


def participants(b: PageBundle) -> list[str]:
    """Known names the model may use in extra mermaid diagrams."""
    names = [diagrams._short(p) for p in b.packages]
    names += [s["qualname"] for s in b.key_symbols]
    return list(dict.fromkeys(names))


def bundle_to_text(b: PageBundle) -> str:
    parts: list[str] = []
    if b.pkg_summaries:
        parts.append("PACKAGES:")
        parts += [f"  - {p}: {s}" for p, s in b.pkg_summaries]
    if b.module_summaries:
        parts.append("\nMODULES:")
        parts += [f"  - {path} [1-{n}]: {s}" for path, s, n in b.module_summaries]
    if b.key_symbols:
        parts.append("\nKEY SYMBOLS:")
        parts += [f"  - {s['kind']} {s['qualname']} — {s['file_path']}:"
                  f"{s['start_line']}-{s['end_line']} — {s['signature']} — {s['summary']}"
                  for s in b.key_symbols]
    if b.excerpts:
        parts.append("\nSOURCE EXCERPTS:")
        for e in b.excerpts:
            parts.append(f"### {e['loc']}\n```\n{e['source']}\n```")
    if b.edges_digest:
        parts.append("\nRELATIONSHIPS (verified edges):")
        parts += [f"  - {e}" for e in b.edges_digest]
    if b.domain_tables:
        parts.append("\nTABLES (reproduce verbatim where the structure calls for them):")
        for t in b.domain_tables:
            parts.append(f"### {t['title']}\n{t['markdown']}")
    if b.git_evidence:
        parts.append("\nRECENT CHANGES (git; pay extra attention to these areas):")
        parts.append(b.git_evidence)
    parts.append("\nCITATION INDEX (the ONLY citable locations; `path:line` must fall inside a range):")
    parts.append(", ".join(b.citation_index))
    return "\n".join(parts)


# ------------------------------------------------------------------ budget trim
def _rebuild_views(b: PageBundle, evidence: list[EvidenceItem]) -> PageBundle:
    """Legacy parallel fields become views of the surviving evidence (eids untouched)."""
    mods = [it.data for it in evidence if it.kind == "module"]
    syms = [it.data for it in evidence if it.kind == "symbol"]
    excerpts = [it.data for it in evidence if it.kind == "excerpt"]
    edges = [line for it in evidence if it.kind == "edges" for line in it.data]
    tables = [it.data for it in evidence if it.kind == "table"]
    pkgs = [it.data for it in evidence if it.kind == "pkg"]
    cites = [f"{p}:1-{n}" for p, _, n in mods]
    cites += [f"{s['file_path']}:{s['start_line']}-{s['end_line']}" for s in syms]
    return replace(b, evidence=evidence, pkg_summaries=pkgs, module_summaries=mods,
                   key_symbols=syms, excerpts=excerpts, edges_digest=edges,
                   domain_tables=tables,
                   citation_index=list(dict.fromkeys(cites))[:MAX_CITATIONS])


def trim_to_budget(b: PageBundle, budget_tokens: int) -> PageBundle:
    """Drop whole low-rank EvidenceItems until the bundle fits; surviving eids stay stable."""
    def cost(bb: PageBundle) -> int:
        return estimate_tokens(bundle_to_text(bb))

    if cost(b) <= budget_tokens:
        return b

    ev = list(b.evidence)

    def keep_first(kind: str, keep: int) -> None:
        nonlocal ev
        idx = [i for i, it in enumerate(ev) if it.kind == kind]
        for i in reversed(idx[keep:]):
            del ev[i]

    # cheapest-loss first: excerpt tails → module tails → symbol/edges tails
    stages = (("excerpt", 3), ("excerpt", 0), ("module", 20), ("module", 12),
              ("symbol", 16), ("edges", 3), ("symbol", 8), ("edges", 1))
    for kind, keep in stages:
        b_view = _rebuild_views(b, ev)
        if cost(b_view) <= budget_tokens:
            return b_view
        keep_first(kind, keep)

    b_view = _rebuild_views(b, ev)
    if cost(b_view) <= budget_tokens:
        return b_view

    # last resort: truncate table rows in place (item survives; eid + title stable)
    new_ev: list[EvidenceItem] = []
    for it in ev:
        if it.kind != "table":
            new_ev.append(it)
            continue
        t = it.data
        lines = t["markdown"].splitlines()
        if len(lines) > 22:
            lines = lines[:22] + [f"| …(+{len(lines) - 22} more rows — see reference page) |"]
        t2 = {**t, "markdown": "\n".join(lines)}
        new_ev.append(replace(it, text=f"### {t2['title']}\n{t2['markdown']}", data=t2))
    return _rebuild_views(b, new_ev)
