"""LLM-mermaid grammar + grounding validation and the deterministic palette pass."""

from codewiki.writer import diagram as D
from codewiki.writer import validate as V

PARTICIPANTS = ["IngestLoop", "Engine.process_event", "TaskQueue"]
EDGES = ["IngestLoop -> Engine.process_event (calls)",
         "Engine.process_event -> TaskQueue (calls)"]

GOOD = '''flowchart TD
  subgraph PY["Python Space"]
    LDM["IngestLoop<br/>ingestion loop"]
  end
  subgraph RS["Engine"]
    RE["Engine.process_event()"]
    OB["TaskQueue"]
  end
  LDM -- "process_event()" --> RE
  RE -- "update_levels()" --> OB
'''


def test_good_block_passes():
    assert V.check_llm_mermaid_block(GOOD, PARTICIPANTS, EDGES) == []


def test_labeled_edge_forms_and_quoted_parens_ok():
    body = ('flowchart TD\n  A["IngestLoop (loop)"]\n  B["TaskQueue"]\n'
            '  A -->|"push(tick)"| B\n  A -. "poll()" .-> B\n')
    errs = V.check_llm_mermaid_block(body, PARTICIPANTS,
                                     ["IngestLoop -> TaskQueue (calls)"])
    assert errs == []


def test_cylinder_and_stadium_node_shapes_accepted():
    # the pw5 prompt encourages data-store cylinders — they must not read as "unparseable"
    body = ('flowchart TD\n  subgraph RUST["Engine"]\n    RE["Engine.process_event()"]\n  end\n'
            '  subgraph STORE["Storage"]\n    ECAN[("jobs:completed")]\n  end\n'
            '  RE -- "XADD jobs:completed" --> ECAN\n')
    assert V.check_llm_mermaid_block(body, ["Engine.process_event", "jobs:completed"]) == []


def test_graph_header_is_banned():
    body = 'graph TD\n  A["IngestLoop"] --> B["TaskQueue"]\n'
    errs = V.check_llm_mermaid_block(body, PARTICIPANTS, EDGES)
    assert any("banned" in e for e in errs)
    body_lr = 'graph LR\n  A["IngestLoop"] --> B["TaskQueue"]\n'
    assert any("banned" in e for e in V.check_llm_mermaid_block(body_lr, PARTICIPANTS, EDGES))


def test_unbalanced_subgraph_rejected():
    body = 'flowchart TD\n  subgraph A["x"]\n  N["IngestLoop"]\n'
    assert any("unbalanced subgraph" in e for e in V.check_llm_mermaid_block(body, PARTICIPANTS))


def test_ungrounded_nodes_rejected():
    body = ('flowchart TD\n  X["KafkaCluster"] --> Y["SparkJob"]\n')
    errs = V.check_llm_mermaid_block(body, PARTICIPANTS, EDGES)
    assert any("unknown" in e for e in errs)


def test_unsupported_edge_rejected():
    body = ('flowchart TD\n  A["TaskQueue"] -- "calls()" --> B["IngestLoop"]\n')
    errs = V.check_llm_mermaid_block(body, PARTICIPANTS,
                                     ["IngestLoop -> Engine.process_event (calls)"])
    assert any("not supported" in e for e in errs)


def test_br_outside_quoted_label_rejected():
    body = 'flowchart TD\n  A["TaskQueue"] --> B["IngestLoop"]\n  <br/> stray\n'
    errs = V.check_llm_mermaid_block(body, PARTICIPANTS)
    assert any("<br/>" in e for e in errs)


def test_sequence_diagram_grammar():
    body = ('sequenceDiagram\n  participant L as IngestLoop\n  participant R as Engine.process_event\n'
            '  L->>R: process_event(tick)\n')
    assert V.check_llm_mermaid_block(body, PARTICIPANTS,
                                     ["IngestLoop -> Engine.process_event (calls)"]) == []


def test_apply_palette_strips_model_styling_and_appends_ours():
    block = ("```mermaid\nflowchart TD\n  subgraph PY[\"Python\"]\n    A[\"IngestLoop\"]\n"
             "  end\n  classDef evil fill:#f00\n  A --> A\n```")
    out = D.apply_palette(block)
    assert "evil" not in out
    assert "classDef cw0" in out and "class A cw0" in out


def test_apply_palette_leaves_sequence_diagrams_unstyled():
    block = "```mermaid\nsequenceDiagram\n  A->>B: hi\n  classDef x fill:#f00\n```"
    out = D.apply_palette(block)
    assert "classDef" not in out
