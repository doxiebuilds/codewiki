"""
diagrams.py — the deterministic architecture diagram, generated from the real code graph.

Every arrow is a real edge (an ``imports``/``calls`` between two internal packages, or a
``publishes``/``consumes`` against a message-bus channel), so the picture is always faithful to
the code — no LLM, no invented edges. The output is a modern ``flowchart TD`` (never ``graph``):

  * **Functional subgraphs, not folders.** Packages are classified into architectural LAYERS
    (configured via ``codewiki.toml``'s ``[[diagram.layers]]``, or a small generic default) and
    each non-empty layer becomes a ``subgraph``. This turns a flat import list into the
    end-to-end data lifecycle.
  * **Cross-boundary flow, no dead ends.** An edge whose destination lives in a *different*
    package — even one outside the page's own set — is kept and its destination is drawn as a
    one-hop "boundary" node, so data is always traced to where it actually goes next.
  * **Semantic labels.** Nodes are named for their component, not their raw path; channel edges
    are labelled with the channel name; resolved calls with the dominant method.
  * **Scannable palette.** Fixed ``classDef`` classes colour each layer (component type), with
    distinct styles for infra (message-bus/DB cylinders) and one-hop boundary nodes.
"""

from __future__ import annotations

import re
import sqlite3

from codewiki import config as C
from codewiki.paths import is_test_path

_SANITIZE = re.compile(r"[^a-zA-Z0-9]+")

MAX_NODES = 20
MAX_PER_LAYER = 4
MAX_BOUNDARY = 4


def _root_prefix() -> str:
    return C.root_prefix()


def _short(pkg: str) -> str:
    prefix = _root_prefix()
    return pkg[len(prefix):] if prefix and pkg.startswith(prefix) else pkg


def _node_id(pkg: str) -> str:
    return "n_" + _SANITIZE.sub("_", _short(pkg)).strip("_")


def _label(pkg: str) -> str:
    """A readable component label: the trailing 1-2 path segments (never the full path)."""
    parts = _short(pkg).split("/")
    if len(parts) >= 2:
        return "/".join(parts[-2:])
    return parts[-1]


# ------------------------------------------------------------------ architectural layers
# Layers are (key, subgraph title, css class, [path substrings]) — first matching substring
# wins, ordered so the subgraphs read top-to-bottom in `flowchart TD`. Configured per-repo via
# ``codewiki.toml``'s ``[[diagram.layers]]`` (see config.py); with no config, every package
# lands in the single "support" bucket — the diagram still renders, just unlayered.
def _layers() -> tuple[tuple[str, str, str, tuple[str, ...]], ...]:
    return tuple((l.key, l.title, l.css_class, l.needles) for l in C.load().diagram_layers)


def _layer_order() -> list[str]:
    return [row[0] for row in _layers()] + ["support"]


def _layer_title(key: str) -> str:
    if key == "support":
        return "Shared Packages"
    return next((row[1] for row in _layers() if row[0] == key), key)


def _layer_class(key: str) -> str:
    if key == "support":
        return "support"
    return next((row[2] for row in _layers() if row[0] == key), key)


# dark-theme palette; layers get colors round-robin from this rotation, plus fixed
# infra/support/boundary classes that always exist regardless of configured layers.
_PALETTE = (
    "fill:#0c4a6e,stroke:#38bdf8,color:#e0f2fe",
    "fill:#7c2d12,stroke:#fb923c,color:#ffedd5",
    "fill:#4a1d6e,stroke:#c084fc,color:#f3e8ff",
    "fill:#14532d,stroke:#22c55e,color:#dcfce7",
    "fill:#134e4a,stroke:#2dd4bf,color:#ccfbf1",
    "fill:#1e293b,stroke:#94a3b8,color:#e2e8f0",
    "fill:#422006,stroke:#eab308,color:#fef9c3",
)


def _classdefs() -> list[str]:
    layers = _layers()
    lines = [f"classDef {cls} {_PALETTE[i % len(_PALETTE)]};"
             for i, (_key, _title, cls, _needles) in enumerate(layers)]
    lines.append("classDef support fill:#292524,stroke:#a8a29e,color:#e7e5e4;")
    lines.append("classDef infra fill:#134e2e,stroke:#4ade80,color:#dcfce7;")
    lines.append("classDef boundary fill:#1f2937,stroke:#6b7280,color:#9ca3af,stroke-dasharray:4 3;")
    return lines


def _layer_of(pkg: str) -> str:
    short = _short(pkg)
    for key, _title, _cls, needles in _layers():
        if any(n in short for n in needles):
            return key
    return "support"


def resolve_import_to_package(dst_name: str, all_packages: set[str]) -> str | None:
    """Map an import string to an internal package dir, or None if external/unresolvable."""
    raw = dst_name.strip()
    m = re.match(r"[a-zA-Z_][\w.]*", raw)
    if not m:
        return None
    dotted = m.group(0)
    candidate = _root_prefix() + dotted.replace(".", "/")
    parts = candidate.split("/")
    for cut in range(len(parts), 1, -1):
        prefix = "/".join(parts[:cut])
        if prefix in all_packages:
            return prefix
    return None


# Generic method names that name-based call resolution latches onto — they carry no
# architectural meaning and, worse, fabricate cross-boundary arrows (a `.remove()` "resolving"
# to some symbol in another package). We never label an edge with these, and never let a call
# edge that only has a generic name INVENT a package pair.
_GENERIC_METHODS = frozenset("""
items keys values get set pop copy append extend update add remove clear count index sort
sorted join split strip lstrip rstrip format encode decode read write open close send recv put
run call start stop wait notify acquire release cancel done result group match search sub
compile now time sleep load dump dumps loads round min max sum abs len map filter zip range
print super replace monotonic mean median commit execute executemany fetchone fetchall
fetchmany cursor connect collect spawn key value lock reduce apply insert delete find exists
to_dict from_dict model_dump dict list tuple str int float bool bytes setdefault get_client
name id type kind size length total start_line end_line
""".split())


def _meaningful(method: str) -> bool:
    """A callee name worth trusting as a cross-package edge/label: domain-specific, not a
    builtin/container/dunder. Heuristic: not generic, not private, and either compound
    (snake_case), a Type (CamelCase), or long enough to be a real verb."""
    m = (method or "").strip()
    if not m or m.startswith("_") or m in _GENERIC_METHODS:
        return False
    return "_" in m or m[:1].isupper() or len(m) >= 7


# ------------------------------------------------------------------ edge collection
def _structural_edges(conn: sqlite3.Connection, packages: set[str], all_packages: set[str],
                      ) -> dict[tuple[str, str], str]:
    """Package→package edges (src in `packages`, dst any internal package). Value = edge label.

    ``imports`` are the reliable structural backbone (unlabelled). Resolved calls with a
    *meaningful* callee name add the data-flow labels — and may introduce a cross-boundary pair
    the imports missed — but generic-name calls (``items``/``round``/``remove`` …) are dropped
    entirely so they neither mislabel nor fabricate arrows. Cross-boundary edges (dst outside
    ``packages``) are KEPT — tracing data to where it actually goes is the whole point.
    """
    qmarks = ",".join("?" * len(packages))
    labels: dict[tuple[str, str], str] = {}

    # backbone: real import dependencies (trustworthy, but unlabelled)
    for r in conn.execute(
            f"SELECT s.package sp, e.dst_name dst, d.package dp FROM edges e "
            f"JOIN symbols s ON s.id=e.src_id LEFT JOIN symbols d ON d.id=e.dst_id "
            f"WHERE e.kind='imports' AND s.package IN ({qmarks})", tuple(packages)):
        src = r["sp"]
        dst = r["dp"] or resolve_import_to_package(r["dst"], all_packages)
        if not dst or dst == src or dst not in all_packages:
            continue
        labels.setdefault((src, dst), "")

    # labels + extra data-flow edges from resolved calls, meaningful names ONLY
    for r in conn.execute(
            f"SELECT s.package sp, dst.package dp, dst.name method FROM edges e "
            f"JOIN symbols s ON s.id=e.src_id JOIN symbols dst ON dst.id=e.dst_id "
            f"WHERE e.kind='calls' AND e.resolved IN ('import','unique') "
            f"AND s.package IN ({qmarks})", tuple(packages)):
        src, dst, method = r["sp"], r["dp"], (r["method"] or "").strip()
        if not dst or dst == src or dst not in all_packages or not _meaningful(method):
            continue
        pair = (src, dst)
        if not labels.get(pair):            # add new pair, or label a previously-bare import
            labels[pair] = f"{method}()"
    return labels


def _channel_edges(conn: sqlite3.Connection, packages: set[str],
                   ) -> list[tuple[str, str, str]]:
    """(package, channel, kind) for publishes/consumes touching the page's packages."""
    qmarks = ",".join("?" * len(packages))
    out: list[tuple[str, str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for r in conn.execute(
            f"SELECT DISTINCT s.package sp, e.kind k, e.dst_name chan FROM edges e "
            f"JOIN symbols s ON s.id=e.src_id "
            f"WHERE e.kind IN ('publishes','consumes') AND s.package IN ({qmarks}) "
            f"AND e.dst_name IS NOT NULL AND e.dst_name != '' "
            f"ORDER BY e.dst_name", tuple(packages)):
        key = (r["sp"], r["chan"], r["k"])
        if key in seen:
            continue
        seen.add(key)
        out.append(key)
    return out


# ------------------------------------------------------------------ rendering
def _emit(node_layer: dict[str, str], node_label: dict[str, str],
          node_shape: dict[str, str], node_class: dict[str, str],
          edges: list[tuple[str, str, str]]) -> str:
    """Render subgraphs (by layer, lifecycle order), then edges, then classDefs + classes."""
    by_layer: dict[str, list[str]] = {}
    for nid, layer in node_layer.items():
        by_layer.setdefault(layer, []).append(nid)

    out = ["```mermaid", "flowchart TD"]
    for layer in _layer_order():
        ids = by_layer.get(layer)
        if not ids:
            continue
        out.append(f'  subgraph {layer}_grp["{_layer_title(layer)}"]')
        out.append("    direction TB")
        for nid in ids:
            lb, rb = node_shape.get(nid, ('["', '"]'))
            out.append(f'    {nid}{lb}{node_label[nid]}{rb}')
        out.append("  end")

    for src, dst, label in edges:
        if label:
            out.append(f'  {src} -- "{label}" --> {dst}')
        else:
            out.append(f"  {src} --> {dst}")

    # classDefs (only the ones we used) + one class assignment per membership bucket
    used_classes = set(node_class.values())
    for cd in _classdefs():
        name = cd.split()[1]
        if name in used_classes:
            out.append(f"  {cd}")
    buckets: dict[str, list[str]] = {}
    for nid, cls in node_class.items():
        buckets.setdefault(cls, []).append(nid)
    for cls, ids in buckets.items():
        out.append(f"  class {','.join(ids)} {cls};")

    out.append("```")
    return "\n".join(out)


def package_dependency_mermaid(conn: sqlite3.Connection, packages: set[str], *,
                               max_edges: int = 18, include_tests: bool = True) -> str:
    """Layered ``flowchart TD`` of the page's packages, their cross-boundary data flow, and the
    Redis channels they touch. Returns "" only when there is nothing to draw.

    ``include_tests=False`` drops test packages from the picture (a non-test page should not
    lead its architecture diagram with test scaffolding).
    """
    if not include_tests:
        packages = {p for p in packages if not is_test_path(p)}
    packages = set(packages)
    if not packages:
        return ""
    all_packages = {r["package"] for r in conn.execute(
        "SELECT DISTINCT package FROM symbols WHERE package IS NOT NULL")}

    structural = _structural_edges(conn, packages, all_packages)
    channels = _channel_edges(conn, packages)

    # ----- rank package nodes by edge degree so we keep the most connected ones under the cap
    degree: dict[str, int] = {}
    for (src, dst) in structural:
        degree[src] = degree.get(src, 0) + 1
        degree[dst] = degree.get(dst, 0) + 1
    for (pkg, _chan, _k) in channels:
        degree[pkg] = degree.get(pkg, 0) + 1

    def _rank(pkg: str) -> tuple[int, int, str]:
        # page packages first, then by degree desc, then name for stability
        return (0 if pkg in packages else 1, -degree.get(pkg, 0), pkg)

    # candidate nodes: page packages + one-hop boundary destinations
    boundary = {dst for (_s, dst) in structural if dst not in packages}
    boundary_nodes = sorted(boundary, key=_rank)[:MAX_BOUNDARY]

    # Keep the top-degree packages PER LAYER, round-robin, so every architectural layer the page
    # touches is represented (the end-to-end spine) instead of one high-degree layer crowding out
    # the rest. Rank 0 of every layer lands before any layer's rank 1.
    ranked: dict[str, list[str]] = {}
    for pkg in sorted(packages, key=_rank):
        ranked.setdefault(_layer_of(pkg), []).append(pkg)
    kept: list[str] = []
    for rank in range(MAX_PER_LAYER):
        for layer in _layer_order():
            bucket = ranked.get(layer, [])
            if rank < len(bucket):
                kept.append(bucket[rank])
        if len(kept) >= MAX_NODES - MAX_BOUNDARY:
            break
    kept = kept[:MAX_NODES - MAX_BOUNDARY]
    node_pkgs = set(kept) | set(boundary_nodes)
    if len(node_pkgs) < 2 and not channels:
        return ""

    node_layer: dict[str, str] = {}
    node_label: dict[str, str] = {}
    node_shape: dict[str, str] = {}
    node_class: dict[str, str] = {}
    for pkg in node_pkgs:
        nid = _node_id(pkg)
        layer = _layer_of(pkg)
        node_layer[nid] = layer
        node_label[nid] = _label(pkg)
        node_shape[nid] = ('["', '"]')
        node_class[nid] = "boundary" if pkg not in packages else _layer_class(layer)

    # ----- draw edges only between drawn nodes (cap total for readability)
    edges: list[tuple[str, str, str]] = []
    for (src, dst), label in sorted(structural.items()):
        if src in node_pkgs and dst in node_pkgs:
            edges.append((_node_id(src), _node_id(dst), label))
            if len(edges) >= max_edges:
                break

    # ----- Redis channels become infra cylinders in the store layer, with labelled flows
    ch_ids: dict[str, str] = {}
    for (pkg, chan, kind) in channels:
        if pkg not in node_pkgs or len(edges) >= max_edges:
            continue
        cid = "ch_" + _SANITIZE.sub("_", chan).strip("_")
        if cid not in ch_ids and len(node_layer) < MAX_NODES + 6:
            ch_ids[cid] = chan
            node_layer[cid] = "store"
            node_label[cid] = chan
            node_shape[cid] = ('[("', '")]')            # cylinder
            node_class[cid] = "infra"
        if cid not in node_layer:
            continue
        pid = _node_id(pkg)
        if kind == "publishes":
            edges.append((pid, cid, chan))
        else:
            edges.append((cid, pid, chan))

    if not edges and len(node_layer) < 2:
        return ""
    return _emit(node_layer, node_label, node_shape, node_class, edges)
