"""
validate.py — post-generation validators + deterministic repair for LLM-written pages.

This module validates at TWO levels:
  * per section (``check_section``): exact heading, a bare-token Sources block whose entries
    survive repair against the section's evidence slice, no inline file paths in prose, no
    preamble/truncation. Blocking errors trigger one section retry, then the section fallback.
  * per LLM diagram (``check_llm_mermaid_block``): extended mermaid grammar (labeled edges,
    subgraphs, ``<br/>`` inside quoted labels) + grounding against known participants/edges.

The earlier page-level pieces (``repair_citations``, ``check_and_fix_mermaid``, ``validate_page``)
are kept: the inline-citation pass is retired for section-based pages but still serves tests and
the deterministic fallback path, and ``check_and_fix_mermaid`` remains the page-level safety net
(det-diagram substitution + 3-block cap).
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field

from codewiki import config as C
from codewiki.writer import sources as SRC

# file-extension-shaped backtick citation, with or without a configured source-subdir prefix
ROOTLESS_CITE_RE = re.compile(
    r"`([\w][\w./\-]*\.(?:py|rs|ts|tsx|js|jsx|sql|ya?ml|toml|json|sh)):(\d+)(?:-(\d+))?`")
MERMAID_RE = re.compile(r"```mermaid\n(.*?)```", re.DOTALL)
_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_MERMAID_HEADERS = ("graph ", "flowchart ", "sequenceDiagram", "stateDiagram")
_BAD_LABEL_RE = re.compile(r"\[[^\]\"\n]*\([^\]\n]*\]")   # unquoted parens inside [label]

MIN_CHARS = 3500
MIN_SECTIONS = 3
MIN_VALID_CITES = 6
MAX_INVALID_RATIO = 0.30


@dataclass
class ValidationReport:
    errors: list[str] = field(default_factory=list)      # blocking → retry/fallback
    warnings: list[str] = field(default_factory=list)    # repaired in place
    markdown: str = ""
    n_citations: int = 0
    n_repaired_citations: int = 0
    n_mermaid_substituted: int = 0


# ------------------------------------------------------------------ citations
def _file_max_line(conn: sqlite3.Connection, path: str) -> int | None:
    row = conn.execute(
        "SELECT MAX(end_line) m FROM symbols WHERE file_path=?", (path,)).fetchone()
    return row["m"] if row and row["m"] else None


def _cite_re() -> re.Pattern:
    """Citations must carry the configured source-subdir prefix, when one is set."""
    prefix = C.root_prefix()
    if not prefix:
        return ROOTLESS_CITE_RE
    p = re.escape(prefix.rstrip("/"))
    return re.compile(rf"`({p}/[\w./\-]+):(\d+)(?:-(\d+))?`")


def repair_citations(conn: sqlite3.Connection, md: str) -> tuple[str, list[str], dict]:
    """Prefix rootless paths, clamp/strip out-of-range lines, strip unknown paths."""
    stats = {"total": 0, "invalid": 0, "repaired": 0}
    known = {r["path"] for r in conn.execute("SELECT path FROM files")}
    prefix = C.root_prefix()

    def _prefix(m: re.Match) -> str:
        cand = f"{prefix}{m.group(1)}"
        if cand in known:
            stats["repaired"] += 1
            rng = f":{m.group(2)}" + (f"-{m.group(3)}" if m.group(3) else "")
            return f"`{cand}{rng}`"
        return m.group(0)

    if prefix:
        md = ROOTLESS_CITE_RE.sub(_prefix, md)

    def _check(m: re.Match) -> str:
        stats["total"] += 1
        path, a = m.group(1), int(m.group(2))
        b = int(m.group(3)) if m.group(3) else None
        if path not in known:
            stats["invalid"] += 1
            return ""                                    # drop hallucinated-path citation
        max_line = _file_max_line(conn, path)
        if max_line is None:
            return m.group(0)
        if a > max_line:
            stats["invalid"] += 1
            stats["repaired"] += 1
            return f"`{path}`"                           # keep the file, drop the bogus line
        if b is not None and b > max_line:
            stats["repaired"] += 1
            return f"`{path}:{a}-{max_line}`"            # clamp the range end
        return m.group(0)

    md = _cite_re().sub(_check, md)
    errors: list[str] = []
    valid = stats["total"] - stats["invalid"]
    if stats["total"] and stats["invalid"] / stats["total"] > MAX_INVALID_RATIO:
        errors.append(f"{stats['invalid']}/{stats['total']} citations were invalid — cite only "
                      "locations from the CITATION INDEX")
    if valid < MIN_VALID_CITES:
        errors.append(f"only {valid} valid `path:line` citations — every architectural claim "
                      "needs one (minimum {0})".format(MIN_VALID_CITES))
    return md, errors, stats


# ------------------------------------------------------------------ mermaid
def _norm(block: str) -> str:
    return "".join(block.split())


def _block_ok(block: str, known: set[str]) -> bool:
    body = block.strip()
    if not body.startswith(_MERMAID_HEADERS):
        return False
    if _BAD_LABEL_RE.search(body):                       # the classic local-model breaker
        return False
    # grounding: identifier-shaped labels/participants should exist in the graph
    idents = re.findall(r'\["([^"]+)"\]|\[([\w./:\- ]+)\]|participant\s+(\w+)', body)
    names = [n for tup in idents for n in tup if n]
    if names:
        unknown = [n for n in names
                   if re.fullmatch(r"[A-Za-z_][\w.:/\-]{3,}", n.strip()) and
                   n.strip() not in known]
        if len(unknown) > len(names) / 2:
            return False
    return True


def check_and_fix_mermaid(md: str, det_diagram: str,
                          known_participants: list[str]) -> tuple[str, list[str], int]:
    """First block must be the deterministic diagram (else substituted); bad extras dropped."""
    warnings: list[str] = []
    substituted = 0
    known = set(known_participants)
    det_body = ""
    if det_diagram:
        m = MERMAID_RE.search(det_diagram)
        det_body = m.group(1) if m else ""

    blocks = list(MERMAID_RE.finditer(md))
    replacements: list[tuple[int, int, str]] = []
    for i, m in enumerate(blocks):
        body = m.group(1)
        if i == 0 and det_body:
            if _norm(body) != _norm(det_body):
                replacements.append((m.start(), m.end(), det_diagram))
                warnings.append("architecture diagram was edited — substituted the verified one")
                substituted += 1
        elif i >= 3:
            replacements.append((m.start(), m.end(), ""))
            warnings.append("dropped extra mermaid block beyond the allowed 3")
        elif not _block_ok(body, known):
            replacements.append((m.start(), m.end(), ""))
            warnings.append(f"dropped invalid mermaid block #{i + 1}")
    for start, end, repl in reversed(replacements):
        md = md[:start] + repl + md[end:]

    if det_diagram and not MERMAID_RE.search(md):
        md = md.rstrip() + f"\n\n## Architecture\n\n{det_diagram}\n"
        warnings.append("no usable mermaid block survived — inserted the verified architecture diagram")
        substituted += 1
    return md, warnings, substituted


# ------------------------------------------------------------------ structure
def check_structure(md: str, title: str) -> list[str]:
    errors: list[str] = []
    stripped = md.strip()
    first = stripped.splitlines()[0].strip() if stripped else ""
    if first != f"# {title}":
        errors.append(f"page must start with exactly `# {title}`")
    if len(re.findall(r"^## ", md, re.MULTILINE)) < MIN_SECTIONS:
        errors.append(f"fewer than {MIN_SECTIONS} `##` sections — group content by workflow")
    if not re.search(r"^##.*(where to start|watch)", md, re.IGNORECASE | re.MULTILINE):
        errors.append("missing the `## Where to start & watch-outs` section")
    if len(stripped) < MIN_CHARS:
        errors.append(f"page too thin ({len(stripped)} chars < {MIN_CHARS}) — expand the "
                      "workflow narrative, do not pad")
    if stripped.count("```") % 2 != 0:
        errors.append("unbalanced code fences (page likely truncated)")
    if re.match(r"(here is|sure|certainly|below is)", stripped, re.IGNORECASE):
        errors.append("remove conversational preamble — raw markdown only")
    return errors


def tidy_prose(md: str) -> str:
    """Collapse stray whitespace before punctuation in prose (skip fenced blocks)."""
    parts = md.split("```")
    for i in range(0, len(parts), 2):                           # even indexes = outside fences
        parts[i] = re.sub(r"[ \t]+([.,;:])", r"\1", parts[i])
    return "```".join(parts)


# ------------------------------------------------------------------ section validation
MIN_SECTION_CHARS = 350
MAX_INLINE_PATHS = 3
MIN_SECTION_SOURCES = 2
MAX_SECTION_SOURCES = 6


@dataclass
class SectionReport:
    errors: list[str] = field(default_factory=list)      # blocking → one retry → fallback
    warnings: list[str] = field(default_factory=list)    # repaired in place
    markdown: str = ""
    n_sources: int = 0
    fallback: bool = False


def check_section(conn: sqlite3.Connection, md: str, sec, allowed: set[str]) -> SectionReport:
    """Validate ONE section against its plan + evidence slice. ``sec`` is a SectionPlan."""
    report = SectionReport()
    md = _THINK_RE.sub("", md or "").strip()
    if md.startswith("```markdown") and md.endswith("```"):
        md = md[len("```markdown"):].rstrip("`").strip()

    if re.match(r"(here is|sure|certainly|below is)", md, re.IGNORECASE):
        report.errors.append("remove conversational preamble — raw markdown only")
    first_line = md.splitlines()[0].strip() if md else ""
    if first_line != f"## {sec.heading}":
        report.errors.append(f"section must start with exactly `## {sec.heading}`")

    body, entries = SRC.split_sources(md)
    kept: list[tuple[str, int, int | None]] = []
    if entries is not None:
        kept, notes = SRC.repair_entries(conn, entries, allowed)
        report.warnings += notes
    if len(kept) < MIN_SECTION_SOURCES:
        # Deterministic repair, not a retry: the evidence assignment IS the ground truth of
        # what this section covers, so synthesize Sources from it. (Some sections — pure
        # package overviews, table-only — have few or no citable locations at all; failing
        # them would be unsatisfiable by construction.)
        synthesized = []
        for tok in sorted(allowed):
            loc = SRC.parse_loc(tok)
            if loc and loc not in kept:
                synthesized.append(loc)
        kept = (kept + synthesized)[:MAX_SECTION_SOURCES]
        if synthesized:
            report.warnings.append(
                f"Sources block {'missing' if entries is None else 'insufficient'} — "
                f"synthesized {len(synthesized)} entries from the section's evidence")
    kept = kept[:MAX_SECTION_SOURCES]

    body, n_inline = SRC.strip_inline_paths(body)
    if n_inline:
        report.warnings.append(f"replaced {n_inline} inline file path(s) with basenames")
    if n_inline > MAX_INLINE_PATHS:
        report.errors.append(f"{n_inline} raw file paths in prose — refer to code by "
                             "backticked symbol/module names instead")
    if len(body.strip()) < MIN_SECTION_CHARS:
        report.errors.append(f"section too thin ({len(body.strip())} chars < "
                             f"{MIN_SECTION_CHARS}) — expand the narrative, do not pad")
    if body.count("```") % 2 != 0:
        report.errors.append("unbalanced code fences (section likely truncated)")

    body = tidy_prose(body.rstrip())
    report.markdown = body + ("\n\n" + SRC.render_bare(kept) if kept else "")
    report.n_sources = len(kept)
    return report


# ------------------------------------------------------------------ LLM mermaid grammar
# node shapes with quoted labels: rectangle `["x"]`, cylinder `[("x")]`, stadium `(["x"])`,
# circle `(("x"))`, subroutine `[["x"]]`, rhombus `{"x"}` — an opener + "text" (closer optional
# for extraction, validated for the standalone form).
_MM_SHAPE_OPEN = r'(?:\[\(|\(\[|\(\(|\[\[|\{|\[|\()'
_MM_SHAPE_CLOSE = r'(?:\)\]|\]\)|\)\)|\]\]|\}|\]|\))'
_MM_NODE_RE = re.compile(rf'^[A-Za-z][\w]*(?:{_MM_SHAPE_OPEN}"[^"]*"{_MM_SHAPE_CLOSE})?$')
_MM_NODE_DEF_RE = re.compile(rf'([A-Za-z][\w]*){_MM_SHAPE_OPEN}"([^"]*)"')
_MM_ARROW_RE = re.compile(
    r'\s*(?:'
    r'--\s*"[^"]*"\s*-{1,2}>'                      # -- "label" -->
    r'|-\.\s*"[^"]*"\s*\.->'                       # -. "label" .->
    r'|(?:-{2,}>|-\.+->|={2,}>)\s*\|\s*"?[^|]*?"?\s*\|'  # -->|"label"|
    r'|-{2,}>|-\.+->|={2,}>'                       # plain -->, -.->, ==>
    r')\s*')
_MM_SUBGRAPH_RE = re.compile(r'^subgraph\s+[A-Za-z][\w]*(?:\["[^"]*"\])?\s*$')
_MM_DIRECTION_RE = re.compile(r'^direction\s+(?:TB|TD|LR|RL|BT)\s*$')
_MM_STYLING_RE = re.compile(r'^(?:classDef|class\s|style\s|linkStyle|%%)')
_MM_SEQ_MSG_RE = re.compile(r'^\w+\s*-{1,2}(?:>>|>|\)|x)\s*\w+\s*:\s*\S')
_MM_SEQ_MISC_RE = re.compile(
    r'^(?:participant\s+\w+(?:\s+as\s+.+)?|actor\s+\w+(?:\s+as\s+.+)?|autonumber'
    r'|activate\s+\w+|deactivate\s+\w+|[Nn]ote\s.+|alt\s.+|else.*|opt\s.+|loop\s.+|end)\s*$')
MAX_UNKNOWN_LABEL_RATIO = 0.30


def _label_known(label: str, participants_low: list[str]) -> bool:
    text = " ".join(label.lower().replace("<br/>", " ").split())
    if not text:
        return True
    for p in participants_low:
        if p in text or text in p:
            return True
        seg = p.rsplit(".", 1)[-1]
        if len(seg) >= 4 and seg in text:
            return True
    return False


def check_llm_mermaid_block(body: str, participants: list[str],
                            edges: list[str] | None = None) -> list[str]:
    """Grammar + grounding for a MODEL-drawn mermaid block (an extended mermaid dialect).

    Grammar: flowchart/sequence header, balanced subgraph/end, labeled-edge forms
    (`-- "label" -->`, `-.->`, `-->|"label"|`), quoted labels (parens/slashes fine inside),
    `<br/>` only inside quoted labels. Grounding: >30% unknown node labels, or (when the
    verified ``edges`` are given) any matched participant→participant arrow with no supporting
    verified edge, rejects the block.
    """
    errors: list[str] = []
    lines = [ln for ln in (body or "").strip().splitlines()]
    if not lines:
        return ["empty mermaid block"]
    header = lines[0].strip()
    is_seq = header.startswith("sequenceDiagram")
    if header.startswith("graph "):
        errors.append("`graph` header is banned — use `flowchart TD` (or `sequenceDiagram`)")
        return errors
    if not (is_seq or header.startswith("flowchart ")):
        errors.append(f"unsupported mermaid header: {header!r}")
        return errors

    # <br/> must live inside quoted labels
    if "<br/>" in re.sub(r'"[^"]*"', "", body):
        errors.append("`<br/>` outside a quoted label")

    node_labels: dict[str, str] = {}
    drawn_edges: list[tuple[str, str]] = []
    n_sub = n_end = 0

    for raw in lines[1:]:
        line = raw.strip()
        if not line or _MM_STYLING_RE.match(line):
            continue                                     # styling stripped by apply_palette
        if is_seq:
            if _MM_SEQ_MSG_RE.match(line):
                m = re.match(r'^(\w+)\s*-{1,2}(?:>>|>|\)|x)\s*(\w+)\s*:', line)
                if m:
                    drawn_edges.append((m.group(1), m.group(2)))
                continue
            if _MM_SEQ_MISC_RE.match(line):
                pm = re.match(r'^(?:participant|actor)\s+(\w+)(?:\s+as\s+(.+))?', line)
                if pm:
                    node_labels[pm.group(1)] = (pm.group(2) or pm.group(1)).strip().strip('"')
                continue
            errors.append(f"unparseable sequence line: {line!r}")
            continue
        if line == "end":
            n_end += 1
            continue
        if line.startswith("subgraph"):
            n_sub += 1
            if not _MM_SUBGRAPH_RE.match(line):
                errors.append(f"malformed subgraph line: {line!r}")
            continue
        if _MM_DIRECTION_RE.match(line):
            continue
        for nid, label in _MM_NODE_DEF_RE.findall(line):
            node_labels[nid] = label
        if _MM_NODE_RE.match(line):
            continue
        if _MM_ARROW_RE.search(line):
            parts = [p for p in _MM_ARROW_RE.split(line) if p]
            if all(_MM_NODE_RE.match(p.strip()) for p in parts) and len(parts) >= 2:
                ids = [re.match(r'[A-Za-z][\w]*', p.strip()).group(0) for p in parts]
                drawn_edges += list(zip(ids, ids[1:]))
                continue
        errors.append(f"unparseable mermaid line: {line!r}")

    if not is_seq and n_sub != n_end:
        errors.append(f"unbalanced subgraph/end ({n_sub} subgraph, {n_end} end)")

    # grounding: labels (or bare ids) must mostly match known participants
    parts_low = [p.lower() for p in participants]
    edge_ids = {i for pair in drawn_edges for i in pair}
    checkable = [node_labels.get(i, i) for i in sorted(edge_ids)] or list(node_labels.values())
    if checkable:
        unknown = [l for l in checkable if not _label_known(l, parts_low)]
        if len(unknown) > len(checkable) * MAX_UNKNOWN_LABEL_RATIO:
            errors.append(f"{len(unknown)}/{len(checkable)} node labels unknown — use only the "
                          "given participants")

    # every participant→participant arrow needs a supporting verified edge
    if edges is not None and drawn_edges:
        edges_low = [e.lower() for e in edges]

        def _match(nid: str) -> str | None:
            label = node_labels.get(nid, nid)
            for p in participants:
                if _label_known(label, [p.lower()]):
                    return p.rsplit(".", 1)[-1].lower()
            return None

        for src, dst in drawn_edges:
            s, d = _match(src), _match(dst)
            if s and d and s != d:
                if not any(s in e and d in e for e in edges_low):
                    errors.append(f"edge {src} -> {dst} is not supported by any verified edge")
    return errors


# ------------------------------------------------------------------ orchestration
def validate_page(conn: sqlite3.Connection, md: str, title: str, det_diagram: str,
                  known_participants: list[str]) -> ValidationReport:
    report = ValidationReport()
    md = _THINK_RE.sub("", md).strip()
    if md.startswith("```markdown") and md.endswith("```"):     # whole-answer fence
        md = md[len("```markdown"):].rstrip("`").strip()

    md, warns, substituted = check_and_fix_mermaid(md, det_diagram, known_participants)
    report.warnings += warns
    report.n_mermaid_substituted = substituted

    md, cite_errors, stats = repair_citations(conn, md)
    report.errors += cite_errors
    report.n_citations = stats["total"]
    report.n_repaired_citations = stats["repaired"]

    md = tidy_prose(md)
    report.errors += check_structure(md, title)
    report.markdown = md
    return report
