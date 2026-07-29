"""
skeleton.py — plan-then-fill stage 1: turn the evidence catalog into a validated page skeleton.

The planner LLM sees ONLY the one-line evidence labels (never the full blocks) and returns a
JSON plan: 5-7 sections, each with a heading, brief, assigned evidence ids, an optional diagram
slot and an optional reference table. ``validate_skeleton`` is hand-rolled (no jsonschema dep):
structural problems that a deterministic fix-up can repair are repaired (missing watch-outs
section, unassigned tables, too many/duplicate/thin sections, misplaced architecture diagram);
what cannot be repaired (unparseable output, wrong types, unknown eids, <4 sections after
fixups) blocks and triggers ONE retry with the error list, then ``fallback_skeleton`` — a
deterministic plan that distributes evidence by kind so the section writers can still run.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field

from codewiki.assembly.pages import PageSpec
from codewiki.writer import bundle as B
from codewiki.writer import prompts

_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_FENCE_RE = re.compile(r"```[a-zA-Z]*\n?|```")
_WATCH_RE = re.compile(r"watch|where to start", re.IGNORECASE)

MAX_SECTIONS = 8
MIN_SECTIONS = 4
MAX_EXTRA_DIAGRAMS = 2
WATCHOUTS_HEADING = "Where to Start & Watch-Outs"
PLANNER_TIMEOUT = 900


@dataclass
class SectionPlan:
    id: str
    heading: str
    brief: str = ""
    subsections: list[str] = field(default_factory=list)
    diagram: dict | None = None
    table: str | None = None
    evidence: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"id": self.id, "heading": self.heading, "brief": self.brief,
                "subsections": self.subsections, "diagram": self.diagram,
                "table": self.table, "evidence": self.evidence}


@dataclass
class Skeleton:
    title: str
    sections: list[SectionPlan]
    source: str = "llm"                          # llm | llm_retry | fallback

    def to_dict(self) -> dict:
        return {"title": self.title, "source": self.source,
                "sections": [s.to_dict() for s in self.sections]}


def _is_watchouts(heading: str) -> bool:
    return bool(_WATCH_RE.search(heading))


# ------------------------------------------------------------------ parsing
def parse_skeleton(text: str) -> dict | None:
    """Strip <think>/fences, brace-scan the outermost JSON object (like S._parse_json)."""
    text = _THINK_RE.sub("", text or "")
    text = _FENCE_RE.sub("", text).strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        data = json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


# ------------------------------------------------------------------ validation + fixups
def _coerce_section(raw: object, idx: int, errors: list[str]) -> SectionPlan | None:
    if not isinstance(raw, dict):
        errors.append(f"section #{idx + 1} is not an object")
        return None
    heading = raw.get("heading")
    if not isinstance(heading, str) or not heading.strip():
        errors.append(f"section #{idx + 1} has no string heading")
        return None
    evidence = raw.get("evidence", [])
    if not isinstance(evidence, list) or not all(isinstance(e, str) for e in evidence):
        errors.append(f'section "{heading}" has a non-list-of-strings "evidence"')
        return None
    subsections = raw.get("subsections") or []
    if not isinstance(subsections, list):
        subsections = []
    diagram = raw.get("diagram")
    if diagram is not None and not isinstance(diagram, dict):
        diagram = None
    table = raw.get("table")
    if table is not None and not isinstance(table, str):
        table = None
    return SectionPlan(
        id=str(raw.get("id") or f"S{idx + 1}"),
        heading=heading.strip().lstrip("#").strip(),
        brief=str(raw.get("brief") or "").strip(),
        subsections=[str(s).strip().lstrip("#").strip() for s in subsections if str(s).strip()],
        diagram=diagram, table=table,
        evidence=list(dict.fromkeys(e.strip() for e in evidence if e.strip())))


def validate_skeleton(data: dict | None, b: B.PageBundle,
                      spec: PageSpec) -> tuple[Skeleton | None, list[str]]:
    """(skeleton, errors). errors non-empty => skeleton is None (blocking, retry-worthy)."""
    errors: list[str] = []
    if not isinstance(data, dict):
        return None, ["output was not a parseable JSON object"]
    raw_sections = data.get("sections")
    if not isinstance(raw_sections, list) or not raw_sections:
        return None, ['missing or empty "sections" array']

    sections: list[SectionPlan] = []
    for i, raw in enumerate(raw_sections):
        sec = _coerce_section(raw, i, errors)
        if sec is not None:
            sections.append(sec)
    if errors:
        return None, errors

    valid_eids = {it.eid for it in b.evidence}
    unknown = sorted({e for sec in sections for e in sec.evidence if e not in valid_eids})
    if unknown:
        return None, [f"unknown evidence ids (not in the catalog): {', '.join(unknown)}"]

    # ---- deterministic fixups ----
    # dedupe headings: merge a later duplicate into the first occurrence
    seen: dict[str, SectionPlan] = {}
    deduped: list[SectionPlan] = []
    for sec in sections:
        key = sec.heading.lower()
        if key in seen:
            first = seen[key]
            first.evidence = list(dict.fromkeys(first.evidence + sec.evidence))
            first.table = first.table or sec.table
            continue
        seen[key] = sec
        deduped.append(sec)
    sections = deduped

    # merge sections with <2 evidence ids into a neighbor (table holders + watch-outs exempt)
    merged: list[SectionPlan] = []
    for sec in sections:
        if (len(sec.evidence) < 2 and merged and sec.table is None
                and not _is_watchouts(sec.heading)):
            prev = merged[-1]
            prev.evidence = list(dict.fromkeys(prev.evidence + sec.evidence))
            continue
        merged.append(sec)
    sections = merged

    # canonical watch-outs section: append if missing, move to the end if misplaced
    watch = [s for s in sections if _is_watchouts(s.heading)]
    if not watch:
        test_eids = [it.eid for it in b.evidence
                     if it.kind == "git" or it.label.startswith("[module:test]")]
        sections.append(SectionPlan(id="", heading=WATCHOUTS_HEADING,
                                    brief="Entry points, invariants and change guidance.",
                                    evidence=test_eids))
    elif not _is_watchouts(sections[-1].heading):
        sections = [s for s in sections if s is not watch[0]] + [watch[0]]

    # unassigned reference tables get a Reference section (before the watch-outs tail)
    assigned = {s.table for s in sections if s.table}
    for t in b.domain_tables:
        if t["title"] in assigned:
            continue
        table_eids = [it.eid for it in b.evidence
                      if it.kind == "table" and it.data is t]
        heading = "Reference" if "reference" not in {s.heading.lower() for s in sections} \
            else f"Reference: {t['title']}"
        sections.insert(len(sections) - 1, SectionPlan(
            id="", heading=heading, brief=f"Reference table: {t['title']}.",
            table=t["title"], evidence=table_eids))

    # every section needs citable evidence (Sources are synthesized from it when the model's
    # own block fails) — top up cite-less sections with the strongest module items
    cited_mods = [it.eid for it in b.evidence if it.kind == "module" and it.cites]
    cites_of = {it.eid: bool(it.cites) for it in b.evidence}
    for sec in sections:
        if not any(cites_of.get(e) for e in sec.evidence) and cited_mods:
            sec.evidence = list(dict.fromkeys(sec.evidence + cited_mods[:2]))

    # cap at MAX_SECTIONS: merge the thinnest middle section into its predecessor
    while len(sections) > MAX_SECTIONS:
        middle = sections[1:-1]
        victim = min(middle, key=lambda s: (len(s.evidence), sections.index(s)))
        i = sections.index(victim)
        prev = sections[i - 1]
        prev.evidence = list(dict.fromkeys(prev.evidence + victim.evidence))
        prev.table = prev.table or victim.table
        del sections[i]

    # exactly one architecture-diagram placeholder, pinned to section 2
    for sec in sections:
        if sec.diagram and sec.diagram.get("type") == "architecture":
            sec.diagram = None
    if b.det_diagram and len(sections) >= 2:
        sections[1].diagram = {"type": "architecture"}
    # cap extra flow/sequence diagrams
    extra = 0
    for sec in sections:
        if sec.diagram and sec.diagram.get("type") != "architecture":
            extra += 1
            if extra > MAX_EXTRA_DIAGRAMS:
                sec.diagram = None

    if len(sections) < MIN_SECTIONS:
        return None, [f"only {len(sections)} usable sections after normalization — plan "
                      f"at least {MIN_SECTIONS} following the runtime flow"]

    for i, sec in enumerate(sections):
        sec.id = f"S{i + 1}"
    return Skeleton(title=spec.title, sections=sections), []


# ------------------------------------------------------------------ deterministic fallback
def fallback_skeleton(spec: PageSpec, b: B.PageBundle) -> Skeleton:
    """Distribute evidence by kind into the canonical section layout (no LLM)."""
    by_kind: dict[str, list[str]] = {}
    test_mods: list[str] = []
    for it in b.evidence:
        if it.kind == "module" and it.label.startswith("[module:test]"):
            test_mods.append(it.eid)
        else:
            by_kind.setdefault(it.kind, []).append(it.eid)

    purpose = list(by_kind.get("pkg", [])) + by_kind.get("module", [])[:2]
    sections = [SectionPlan(id="", heading="Purpose and Scope",
                            brief="What this area does and why it exists.",
                            evidence=list(dict.fromkeys(purpose)))]

    arch_evidence = by_kind.get("edges", []) + by_kind.get("module", [])[:2]
    if b.det_diagram or arch_evidence:
        sections.append(SectionPlan(
            id="", heading="Architecture",
            brief="How the packages fit together at runtime.",
            diagram={"type": "architecture"} if b.det_diagram else None,
            evidence=list(dict.fromkeys(arch_evidence))))

    components = by_kind.get("module", []) + by_kind.get("symbol", [])
    if components:
        sections.append(SectionPlan(id="", heading="Key Components",
                                    brief="The load-bearing modules and symbols.",
                                    evidence=list(dict.fromkeys(components))))

    workflows = by_kind.get("excerpt", []) + by_kind.get("symbol", [])[:2]
    if workflows:
        sections.append(SectionPlan(id="", heading="Runtime Workflows",
                                    brief="What actually happens when this code runs.",
                                    evidence=list(dict.fromkeys(workflows))))

    if b.domain_tables:
        table_eids = by_kind.get("table", [])
        sections.append(SectionPlan(id="", heading="Reference",
                                    brief="Reference tables extracted from the code graph.",
                                    table=b.domain_tables[0]["title"], evidence=table_eids))

    sections.append(SectionPlan(id="", heading=WATCHOUTS_HEADING,
                                brief="Entry points, invariants and change guidance.",
                                evidence=test_mods + by_kind.get("git", [])))

    for i, sec in enumerate(sections):
        sec.id = f"S{i + 1}"
    return Skeleton(title=spec.title, sections=sections, source="fallback")


# ------------------------------------------------------------------ driver
def plan_page(conn: sqlite3.Connection, spec: PageSpec, b: B.PageBundle, *, chat_fn,
              model: str, max_tokens: int = 900) -> tuple[Skeleton, dict]:
    """One planner call → validate → one retry with the error list → fallback skeleton.

    A transport failure on the FIRST call propagates to the caller (LLM unreachable → the page
    orchestrator decides); a failure on the retry degrades to the fallback skeleton.
    """
    totals = {"prompt_tokens": 0, "completion_tokens": 0}
    user = prompts.planner_user(spec.title, spec.slug, B.evidence_catalog(b),
                                [t["title"] for t in b.domain_tables], b.git_evidence)
    text, usage = chat_fn(user, model=model, system=prompts.PLANNER_SYSTEM,
                          max_tokens=max_tokens, timeout=PLANNER_TIMEOUT)
    totals["prompt_tokens"] += usage.get("prompt_tokens", 0)
    totals["completion_tokens"] += usage.get("completion_tokens", 0)
    skel, errors = validate_skeleton(parse_skeleton(text), b, spec)
    if skel is not None:
        return skel, totals

    try:
        text2, usage2 = chat_fn(prompts.planner_retry_user(text, errors), model=model,
                                system=prompts.PLANNER_SYSTEM, max_tokens=max_tokens,
                                timeout=PLANNER_TIMEOUT)
    except Exception:
        return fallback_skeleton(spec, b), totals
    totals["prompt_tokens"] += usage2.get("prompt_tokens", 0)
    totals["completion_tokens"] += usage2.get("completion_tokens", 0)
    skel, _ = validate_skeleton(parse_skeleton(text2), b, spec)
    if skel is not None:
        skel.source = "llm_retry"
        return skel, totals
    return fallback_skeleton(spec, b), totals
