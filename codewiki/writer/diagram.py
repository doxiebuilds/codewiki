"""
diagram.py — plan-then-fill stage 3: draw the planned extra flow/sequence diagrams.

The deterministic architecture diagram never comes from a model; only the skeleton's optional
flow/sequence slots do. Each one gets a single LLM call fed ONLY the section's participants and
the verified edges between them (``relevant_edges``); the output must survive
``validate.check_llm_mermaid_block`` (grammar + grounding) or the slot yields ``""`` and the
placeholder line is deleted — a failed diagram never degrades the page. ``apply_palette``
strips any model styling and appends our fixed dark-theme classDefs keyed by subgraph
membership, so every shipped diagram looks like ours.
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import PurePosixPath

from codewiki.assembly import diagrams as DET
from codewiki.writer import bundle as B
from codewiki.writer import prompts, validate
from codewiki.writer.skeleton import SectionPlan

DIAGRAM_TIMEOUT = 300
MAX_PARTICIPANTS = 14
_STYLING_LINE_RE = re.compile(r"^\s*(?:classDef\b|class\s|style\s|linkStyle\b|%%)")
_SUBGRAPH_RE = re.compile(r"^\s*subgraph\s+([A-Za-z][\w]*)")

# fixed dark-theme palette, one class per subgraph (cycled) — same colour family as the
# deterministic architecture anchor (assembly/diagrams.py) so every diagram reads consistently:
# blue (source/IO) · orange (active processing) · purple (compute) · green (storage/stable).
PALETTE = (
    "classDef cw0 fill:#0c4a6e,stroke:#38bdf8,color:#e0f2fe",
    "classDef cw1 fill:#7c2d12,stroke:#fb923c,color:#ffedd5",
    "classDef cw2 fill:#4a1d6e,stroke:#c084fc,color:#f3e8ff",
    "classDef cw3 fill:#14532d,stroke:#22c55e,color:#dcfce7",
)


def relevant_edges(conn: sqlite3.Connection, participants: list[str], b: B.PageBundle,
                   limit: int = 25) -> list[str]:
    """Verified arrows touching the given participants: resolved calls + channel flows."""
    keys = set()
    for p in participants:
        keys.add(p.lower())
        keys.add(p.rsplit(".", 1)[-1].lower())

    def _hit(qualname: str) -> bool:
        q = (qualname or "").lower()
        return q in keys or q.rsplit(".", 1)[-1] in keys

    qmarks = ",".join("?" * len(b.packages))
    out: list[str] = []
    for r in conn.execute(
            f"SELECT DISTINCT src.qualname sq, dst.qualname dq, dst.name dn "
            f"FROM edges e JOIN symbols src ON src.id=e.src_id "
            f"JOIN symbols dst ON dst.id=e.dst_id "
            f"WHERE e.kind='calls' AND e.resolved IN ('import','unique') "
            f"AND src.package IN ({qmarks}) ORDER BY sq, dq", tuple(b.packages)):
        if _hit(r["sq"]) or _hit(r["dq"] or r["dn"]):
            out.append(f"{r['sq'] or '<module>'} -> {r['dq'] or r['dn']} (calls)")
            if len(out) >= limit:
                return out
    for r in conn.execute(
            f"SELECT DISTINCT s.qualname sq, e.kind k, e.dst_name chan "
            f"FROM edges e JOIN symbols s ON s.id=e.src_id "
            f"WHERE e.kind IN ('publishes','consumes') AND s.package IN ({qmarks}) "
            f"ORDER BY e.dst_name", tuple(b.packages)):
        if _hit(r["sq"]):
            arrow = "->" if r["k"] == "publishes" else "<-"
            out.append(f"{r['sq'] or '<module>'} {arrow} redis[{r['chan']}] ({r['k']})")
            if len(out) >= limit:
                break
    return out


def _section_participants(sec: SectionPlan, b: B.PageBundle) -> tuple[list[str], list[str]]:
    """(display lines "name — kind — file", bare names) from the section's evidence."""
    want = set(sec.evidence)
    display: list[str] = []
    names: list[str] = []
    for it in b.evidence:
        if it.eid not in want:
            continue
        if it.kind == "symbol":
            s = it.data
            display.append(f"{s['qualname']} — {s['kind']} — {s['file_path']}")
            names.append(s["qualname"])
        elif it.kind == "module":
            path = it.data[0]
            name = PurePosixPath(path).name
            display.append(f"{name} — module — {path}")
            names.append(name)
        elif it.kind == "pkg":
            short = it.data[0]
            display.append(f"{short} — package — {short}")
            names.append(short)
    return display[:MAX_PARTICIPANTS], list(dict.fromkeys(names))[:MAX_PARTICIPANTS]


def _subgraph_hint(sec: SectionPlan, b: B.PageBundle) -> list[str]:
    groups: list[str] = []
    want = set(sec.evidence)
    for it in b.evidence:
        if it.eid not in want:
            continue
        path = None
        if it.kind == "symbol":
            path = it.data["file_path"]
        elif it.kind == "module":
            path = it.data[0]
        if path:
            pkg = DET._short(str(PurePosixPath(path).parent))
            if pkg not in groups:
                groups.append(pkg)
    return groups[:4]


def apply_palette(block: str) -> str:
    """Strip model styling lines, then append OUR classDefs (class per subgraph membership)."""
    m = validate.MERMAID_RE.search(block)
    body = m.group(1) if m else block
    lines = [l for l in body.splitlines() if not _STYLING_LINE_RE.match(l)]
    header = lines[0].strip() if lines else ""
    if header.startswith("sequenceDiagram"):             # sequence diagrams take no classes
        return "```mermaid\n" + "\n".join(lines).strip() + "\n```"

    # collect node ids per subgraph
    per_subgraph: list[list[str]] = []
    current: list[str] | None = None
    for line in lines[1:]:
        s = line.strip()
        if _SUBGRAPH_RE.match(s):
            current = []
            per_subgraph.append(current)
            continue
        if s == "end":
            current = None
            continue
        if current is not None:
            for nid, _ in validate._MM_NODE_DEF_RE.findall(s):
                if nid not in current:
                    current.append(nid)

    styled = list(lines)
    used = sorted({i % len(PALETTE) for i, ids in enumerate(per_subgraph) if ids})
    for i in used:
        styled.append(f"  {PALETTE[i]}")
    for i, ids in enumerate(per_subgraph):
        if ids:
            styled.append(f"  class {','.join(ids)} cw{i % len(PALETTE)}")
    return "```mermaid\n" + "\n".join(styled).strip() + "\n```"


def fill_diagram(sec: SectionPlan, b: B.PageBundle, conn: sqlite3.Connection, *, chat_fn,
                 model: str, max_tokens: int = 700) -> tuple[str, dict]:
    """One LLM call for one planned flow/sequence slot. ("", usage) on ANY failure."""
    display, names = _section_participants(sec, b)
    if not names:
        return "", {}
    edges = relevant_edges(conn, names, b)
    kind = (sec.diagram or {}).get("type", "flow")
    caption = (sec.diagram or {}).get("caption", "")
    user = prompts.diagram_user(caption, sec.heading, display, edges,
                                _subgraph_hint(sec, b), kind)
    try:
        text, usage = chat_fn(user, model=model, system=prompts.DIAGRAM_SYSTEM,
                              max_tokens=max_tokens, timeout=DIAGRAM_TIMEOUT)
    except Exception:
        return "", {}
    m = validate.MERMAID_RE.search(text or "")
    if not m:
        return "", usage
    body = m.group(1)
    if validate.check_llm_mermaid_block(body, names, edges):
        return "", usage
    return apply_palette(f"```mermaid\n{body}```"), usage
