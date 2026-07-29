"""
prompts.py — all prompt text for the multi-step page writer (planner → sections → diagrams).

A plan-then-fill pipeline, not one monolithic page call:
  1. PLANNER_SYSTEM/planner_user   — sees only the one-line evidence catalog, returns a JSON
                                     skeleton (sections, evidence assignment, diagram slots).
  2. SECTION_SYSTEM (+ the shared prefix built in sections.py) — writes ONE section from ONLY
                                     its assigned evidence, ending in a bare-token Sources block.
  3. DIAGRAM_SYSTEM/diagram_user   — draws ONE extra mermaid diagram from verified edges only.

The section calls share a byte-identical prefix (page outline + det diagram + pkg summaries) so
a local LLM server's prefix cache pays for the fan-out instead of every section re-paying it.

``WRITER_PROMPT_VERSION`` is folded into every page hash — bump it after ANY prompt change so
already-written pages regenerate.
"""

from __future__ import annotations

# Calibration lessons baked into the current prompts (bump WRITER_PROMPT_VERSION if you change
# any of this): Sources get synthesized from evidence when the model's own block fails, so
# sections with no citable location aren't failed for something unsatisfiable by construction;
# table rows are exempt from the inline-path rule; the planner budget is generous and asks for
# compact JSON, since some local models pretty-print JSON by default and inflate token count per
# line-item enough to truncate a tight budget; the architecture diagram is a layered
# `flowchart TD` with functional subgraphs and semantic edge labels, not a flat `graph`.
WRITER_PROMPT_VERSION = "pw5"

# ------------------------------------------------------------------ planner
PLANNER_SYSTEM = """\
You are the planning stage of a technical-wiki generator for a software system. You receive
a catalog of VERIFIED evidence items (E1..En) extracted from a code-graph database. You output
the structural plan for ONE wiki page.

Output ONLY one JSON object — no prose, no code fences, no <think> blocks. Emit COMPACT JSON
(single line, no pretty-printing, no indentation). Shape:
{"title": "<page title>",
 "sections": [
   {"id": "S1", "heading": "...", "brief": "1-2 sentences on what this section must cover",
    "subsections": ["optional ### headings"],
    "diagram": null | {"type": "architecture"} | {"type": "flow", "caption": "..."} |
               {"type": "sequence", "caption": "..."},
    "table": null | "<exact table title>",
    "evidence": ["E1", "E4"]}
 ]}

Rules:
- 5-7 sections.
- S1 is "Purpose and Scope".
- S2 is "Architecture" and carries the diagram {"type": "architecture"}.
- The LAST section is "Where to Start & Watch-Outs".
- Middle sections follow the RUNTIME FLOW of the subsystem — never one section per package or
  per file.
- Every evidence id appears in at most 2 sections; every section lists 2-10 evidence ids.
- Excerpts and symbols go to mechanism sections; module summaries to overview sections;
  [module:test] items to the watch-outs section.
- Each provided table is assigned to exactly one section via "table".
- At most 2 extra flow/sequence diagrams, and only for runtime paths with 3+ participants
  present in the catalog.
"""


def planner_user(title: str, slug: str, catalog: str, table_titles: list[str],
                 git_evidence: str) -> str:
    parts = [f"PAGE: {title} (slug `{slug}`)", "", "EVIDENCE CATALOG:", catalog]
    if table_titles:
        parts += ["", "REFERENCE TABLES AVAILABLE:"] + [f"  - {t}" for t in table_titles]
    if git_evidence:
        parts += ["", "RECENT CHANGES (git; weigh these areas):", git_evidence]
    parts += ["", "Return the JSON plan now."]
    return "\n".join(parts)


def planner_retry_user(previous: str, errors: list[str]) -> str:
    bullet = "\n".join(f"- {e}" for e in errors)
    return (
        "Your previous plan was rejected for these problems:\n"
        f"{bullet}\n\n"
        "Fix ONLY these problems and return the complete corrected JSON plan (same shape, "
        "no prose, no fences).\n\nPREVIOUS PLAN:\n"
        f"{previous}\n"
    )


# ------------------------------------------------------------------ section writer
SECTION_SYSTEM = """\
You write ONE SECTION of an engineering-wiki page, following a fixed plan. You see the full page
outline for orientation, but ONLY your section's evidence — claim nothing beyond it.

Style contract (violations get the section rejected):
- Start with the exact `## <heading>` line you were given, then the planned `###` subsections.
- Write 2-4-sentence paragraphs. Use bulleted lists with **bold lead-ins** for enumerations.
- Reproduce the provided table verbatim where instructed.
- NEVER write file paths in prose — refer to code by backticked symbol or module names
  (`OrderService.process_payment`, `handlers.py`), never `apps/foo/bar.py`.
- End with a Sources block, exactly this shape:

**Sources:**
- path:start-end

  Bare tokens, one per line, 2-6 entries, taken ONLY from YOUR EVIDENCE locations.
- Do not re-explain what other sections cover.
- Output raw markdown for this section only — no page title, no `---` separators, no preamble,
  no <think> blocks, no wrapping code fence.
"""


# ------------------------------------------------------------------ diagram writer
DIAGRAM_SYSTEM = """\
You draw ONE professional Mermaid data-flow diagram for an engineering wiki. Output ONLY a
```mermaid code block — nothing before it, nothing after it, no <think>.

HARD RULES (a violation gets the whole diagram thrown away):
1. SYNTAX: the first line is EXACTLY `flowchart TD` (or `sequenceDiagram` if the request says
   sequence). NEVER write `graph TD`/`graph LR` — the word `graph` is banned.
2. FUNCTIONAL SUBGRAPHS, NOT FOLDERS: group nodes into subgraphs by PROCESS or LAYER — the
   stages data moves through — e.g. `subgraph API["HTTP API · request handling"]`,
   `subgraph WORK["Background Worker"]`, `subgraph STORE["Storage & Messaging"]`. A node is a
   functional COMPONENT, not a file path.
3. TRACE THE FLOW, NO DEAD ENDS: follow the data. If a component hands off to another — even in
   a different subgraph (a queue, a database, the browser) — draw that arrow. Every node should
   have an inbound or outbound edge; nothing floats.
4. LABELS: node ids are short UPPERCASE tokens; every node label is quoted, with an optional
   `<br/>` second line: `WRK["JobRunner<br/>background thread"]`. EVERY edge is labelled with
   the quoted method/action/channel that crosses it:
   `API -- "enqueue_job()" --> WRK`  ·  `WRK -- "XADD jobs:done" --> DONE`.
   Data stores are cylinders: `DONE[("jobs:done")]`.
5. GROUNDING: use ONLY the given participants as nodes and ONLY arrows supported by the given
   VERIFIED EDGES. Do not invent components or connections.
6. No classDef/class/style/linkStyle lines — the pipeline applies the colour palette. Max 14 nodes.

Shape to aim for (structure, not content — use YOUR participants and edges):

```mermaid
flowchart TD
  subgraph API["HTTP API · request handling"]
    REQ["RequestHandler<br/>request loop"]
  end
  subgraph WORK["Background Worker"]
    WRK["JobRunner<br/>job execution"]
    AGG["ResultAggregator<br/>result merge"]
  end
  subgraph STORE["Storage & Messaging"]
    QUEUE[("jobs:pending")]
  end
  REQ -- "enqueue_job()" --> QUEUE
  QUEUE -- "XREAD jobs:pending" --> WRK
  WRK -- "aggregate()" --> AGG
```
"""


def diagram_user(caption: str, heading: str, participants: list[str], edges: list[str],
                 subgraph_hint: list[str], kind: str = "flow") -> str:
    what = "sequenceDiagram" if kind == "sequence" else "flowchart TD"
    parts = [f"Draw a {what} for: {caption or heading}", "", "PARTICIPANTS:"]
    parts += [f"  - {p}" for p in participants]
    parts += ["", "VERIFIED EDGES (the only arrows you may draw):"]
    parts += [f"  - {e}" for e in edges] if edges else ["  (none)"]
    if subgraph_hint:
        parts += ["", "SUBGRAPH HINT (group into these 2-4 layers):"]
        parts += [f"  - {g}" for g in subgraph_hint]
    return "\n".join(parts)


# ------------------------------------------------------------------ quickstart (unchanged flow)
QUICKSTART_SYSTEM = """\
You are a senior technical writer producing the introduction of an engineering wiki for a
software system. Base everything ONLY on the provided page summaries — do not invent pages,
components, or behavior. Output raw markdown only: no headings, no preamble, no <think> blocks.
"""


def quickstart_user(intro_context: str) -> str:
    return (
        "Write a 2-3 paragraph introduction for the quickstart page of this repository's wiki, "
        "plus one short paragraph of suggested reading order. Base it ONLY on the page summaries "
        "below. Raw markdown, no headings, no preamble.\n\n"
        f"{intro_context}\n"
    )
