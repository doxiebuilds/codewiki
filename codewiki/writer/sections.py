"""
sections.py — plan-then-fill stage 2: write each planned section with ONLY its evidence.

Every section call is ``shared_prefix + section_tail``: the prefix (page title, full outline,
the deterministic architecture diagram, package summaries) is BYTE-IDENTICAL across a page's
section calls — nothing dynamic, no dates — so LM Studio's prompt-prefix cache makes the
fan-out cheap. The tail carries the section's plan, its table/diagram instructions, the
do-not-repeat list, and the full evidence slice.

A section that fails ``validate.check_section`` retries ONCE with the error list appended to
the tail (same prefix); truncation (finish_reason=="length") skips the retry. The final resort
is ``section_fallback`` — a deterministic bullet rendering of the section's evidence one-liners
with Sources derived from the slice's own locations, so a page always assembles.
"""

from __future__ import annotations

import re
import sqlite3

from codewiki.writer import bundle as B
from codewiki.writer import prompts, validate
from codewiki.writer import sources as SRC
from codewiki.writer.skeleton import SectionPlan, Skeleton

SECTION_TIMEOUT = 900
MIN_PAGE_CHARS = 3500
MIN_PAGE_SECTIONS = 4
DUP_NGRAM = 8
DUP_OVERLAP = 0.6
_DIAGRAM_TOKEN_RE = re.compile(r"\[\[DIAGRAM:[^\]]+\]\]")


# ------------------------------------------------------------------ prompts
def build_shared_prefix(spec, skeleton: Skeleton, b: B.PageBundle) -> str:
    """The byte-identical prompt prefix shared by ALL of this page's section calls."""
    parts = [f"PAGE: {skeleton.title} (slug `{spec.slug}`)", "", "FULL PAGE OUTLINE:"]
    for sec in skeleton.sections:
        brief = f" — {sec.brief}" if sec.brief else ""
        parts.append(f"  {sec.id}: {sec.heading}{brief}")
    if b.det_diagram:
        parts += ["", "ARCHITECTURE DIAGRAM (already placed in S2; reference it in prose, "
                      "never re-draw it):", b.det_diagram]
    if b.pkg_summaries:
        parts += ["", "PACKAGE SUMMARIES (context only — never cite from this):"]
        parts += [f"  - {p}: {s}" for p, s in b.pkg_summaries]
    parts.append("")
    return "\n".join(parts)


def section_tail(sec: SectionPlan, skeleton: Skeleton, b: B.PageBundle) -> str:
    parts = ["=== YOUR SECTION ===", f"HEADING: {sec.heading}"]
    if sec.brief:
        parts.append(f"BRIEF: {sec.brief}")
    if sec.subsections:
        parts.append("PLANNED SUBSECTIONS: " + "; ".join(f"### {s}" for s in sec.subsections))
    if sec.table:
        table = next((t for t in b.domain_tables if t["title"] == sec.table), None)
        if table:
            parts += [f'TABLE: reproduce this table VERBATIM under a fitting subheading:',
                      table["markdown"]]
    if sec.diagram:
        parts.append(
            f"DIAGRAM: after your first paragraph, put the literal placeholder line "
            f"[[DIAGRAM:{sec.id}]] on its own line — the pipeline replaces it with a "
            f"verified diagram. Do not draw a mermaid block yourself.")
    done = skeleton.sections[:skeleton.sections.index(sec)]
    if done:
        parts.append("Sections already written (do not repeat): "
                     + ", ".join(f"{s.id} {s.heading}" for s in done))
    parts += ["", "YOUR EVIDENCE (claim and cite ONLY from this):",
              B.evidence_slice_text(b, sec.evidence) or "(no direct evidence — keep it short)"]
    allowed = sorted(B.allowed_cites(b, sec.evidence))
    if allowed:
        parts += ["", "End with the Sources block, choosing 2-6 of EXACTLY these locations "
                      "(bare tokens, one `- ` bullet per line):",
                  "\n".join(f"  {t}" for t in allowed[:12])]
    return "\n".join(parts)


# ------------------------------------------------------------------ deterministic fallback
def section_fallback(sec: SectionPlan, b: B.PageBundle) -> str:
    """Bullet rendering of the section's evidence one-liners (no LLM, always valid)."""
    lines = [f"## {sec.heading}", ""]
    if sec.brief:
        lines += [sec.brief, ""]
    want = set(sec.evidence)
    cites: list[str] = []
    for it in b.evidence:
        if it.eid not in want:
            continue
        head, _, rest = it.label.partition(": ")
        lines.append(f"- **{head}** — {rest}" if rest else f"- **{it.label}**")
        cites += it.cites
    if sec.table:
        table = next((t for t in b.domain_tables if t["title"] == sec.table), None)
        if table:
            lines += ["", table["markdown"]]
    entries = []
    for tok in dict.fromkeys(cites):
        loc = SRC.parse_loc(tok)
        if loc:
            entries.append(loc)
    if entries:
        lines += ["", SRC.render_bare(entries[:validate.MAX_SECTION_SOURCES])]
    return "\n".join(lines).rstrip() + "\n"


# ------------------------------------------------------------------ per-section driver
def fill_section(conn: sqlite3.Connection, sec: SectionPlan, skeleton: Skeleton,
                 shared_prefix: str, b: B.PageBundle, *, chat_fn, model: str,
                 max_tokens: int = 1400) -> tuple[str, dict, dict]:
    """(markdown, usage_totals, info). info = {errors, warnings, fallback, retried}."""
    allowed = B.allowed_cites(b, sec.evidence)
    tail = section_tail(sec, skeleton, b)
    user = shared_prefix + tail
    totals = {"prompt_tokens": 0, "completion_tokens": 0}
    info: dict = {"errors": [], "warnings": [], "fallback": False, "retried": False}

    for attempt in (1, 2):
        try:
            text, usage = chat_fn(user, model=model, system=prompts.SECTION_SYSTEM,
                                  max_tokens=max_tokens, timeout=SECTION_TIMEOUT)
        except Exception as exc:
            info["errors"].append(f"LLM call failed: {exc}")
            break
        totals["prompt_tokens"] += usage.get("prompt_tokens", 0)
        totals["completion_tokens"] += usage.get("completion_tokens", 0)
        report = validate.check_section(conn, text, sec, allowed)
        info["warnings"] += report.warnings
        if not report.errors:
            info["errors"] = []
            return report.markdown, totals, info
        info["errors"] = list(report.errors)
        if usage.get("finish_reason") == "length" or attempt == 2:
            break                                       # truncation: retrying won't help
        info["retried"] = True
        bullets = "\n".join(f"- {e}" for e in report.errors)
        user = (shared_prefix + tail
                + "\n\nYOUR PREVIOUS ATTEMPT WAS REJECTED FOR:\n" + bullets
                + "\nFix ONLY these problems and re-emit the complete section.")

    info["fallback"] = True
    return section_fallback(sec, b), totals, info


# ------------------------------------------------------------------ page assembly
def _insert_after_first_paragraph(md: str, block: str) -> str:
    paras = md.split("\n\n")
    idx = len(paras) - 1
    for i, p in enumerate(paras):
        s = p.strip()
        if s and not s.startswith("#"):
            idx = i
            break
    paras.insert(idx + 1, block)
    return "\n\n".join(paras)


def _shingles(text: str, n: int = DUP_NGRAM) -> set[str]:
    words = re.findall(r"[a-z0-9]+", text.lower())
    return {" ".join(words[i:i + n]) for i in range(len(words) - n + 1)}


def assemble_page(conn: sqlite3.Connection, title: str,
                  sections_md: list[tuple[SectionPlan, str]],
                  diagrams: dict[str, str] | None = None) -> tuple[str, list[str], list[str]]:
    """Join sections under `# title`, resolve [[DIAGRAM:Sx]] tokens, run whole-page checks,
    then linkify all bare Sources tokens LAST. Returns (markdown, errors, warnings)."""
    diagrams = diagrams or {}
    errors: list[str] = []
    warnings: list[str] = []

    parts: list[str] = []
    for sec, md in sections_md:
        token = f"[[DIAGRAM:{sec.id}]]"
        block = diagrams.get(sec.id, "")
        if token in md:
            if block:
                md = md.replace(token, block)
            else:                                        # failed diagram → delete its line
                md = "\n".join(l for l in md.splitlines() if token not in l)
        elif block:                                      # planned but token missing → insert
            md = _insert_after_first_paragraph(md, block)
        # any stray tokens for other sections are noise — drop their lines
        md = "\n".join(l for l in md.splitlines() if not _DIAGRAM_TOKEN_RE.search(l))
        parts.append(md.strip())

    page = f"# {title}\n\n" + "\n\n---\n\n".join(parts) + "\n"

    if page.count("```") % 2 != 0:
        errors.append("unbalanced code fences in the assembled page")
    n_sections = len(re.findall(r"^## ", page, re.MULTILINE))
    if n_sections < MIN_PAGE_SECTIONS:
        errors.append(f"only {n_sections} `##` sections assembled (min {MIN_PAGE_SECTIONS})")
    if len(page.strip()) < MIN_PAGE_CHARS:
        errors.append(f"page too thin ({len(page.strip())} chars < {MIN_PAGE_CHARS})")
    n_sources = sum(1 for line in page.splitlines() if SRC.ENTRY_RE.match(line))
    if n_sources < validate.MIN_VALID_CITES:
        errors.append(f"only {n_sources} Sources entries page-wide "
                      f"(min {validate.MIN_VALID_CITES})")

    # near-duplicate sections (normalized 8-gram overlap)
    bodies = [SRC.split_sources(md)[0] for _, md in sections_md]
    grams = [_shingles(body) for body in bodies]
    for i in range(len(grams)):
        for j in range(i + 1, len(grams)):
            if not grams[i] or not grams[j]:
                continue
            overlap = len(grams[i] & grams[j]) / min(len(grams[i]), len(grams[j]))
            if overlap > DUP_OVERLAP:
                warnings.append(f"sections {sections_md[i][0].id} and {sections_md[j][0].id} "
                                f"are near-duplicates (8-gram overlap {overlap:.2f})")

    return SRC.linkify_page(conn, page), errors, warnings
