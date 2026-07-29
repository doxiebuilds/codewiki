"""
scaffold.py — propose a starter ``pages.yaml`` from an already-indexed code graph.

The bundled ``pages.example.yaml`` assumes a conventional layout (``src/``, ``app/``, ``api/``,
``db/``, ``frontend/``, ``tests/``). A repo that doesn't use those directory names matches no
packages at all, every page is skipped, and ``assemble`` writes zero pages — a confusing first
run. ``codewiki init`` reads the graph instead and emits ``include`` prefixes built from the
directories the repo actually has.

Grouping is deterministic: take the shallowest package depth that splits the repo into more than
one meaningful group, and give each group a page. Domain kinds (routes, env flags, ...) are
attached to whichever proposed page contains the most nodes of that kind.
"""

from __future__ import annotations

import re
import sqlite3
from collections import Counter

from codewiki import config as C
from codewiki.paths import is_test_path

MIN_SYMBOLS = 5          # a group smaller than this doesn't earn its own page
MAX_PAGES = 8            # keep the starter taxonomy readable
MAX_DEPTH = 3


def package_counts(conn: sqlite3.Connection) -> dict[str, int]:
    """Symbol count per package path, skipping test packages (they get their own page)."""
    return {
        r["package"]: r["n"]
        for r in conn.execute(
            "SELECT package, COUNT(*) n FROM symbols WHERE package IS NOT NULL AND package != '' "
            "GROUP BY package")
        if not is_test_path(r["package"])
    }


def _groups_at_depth(counts: dict[str, int], depth: int) -> Counter:
    groups: Counter = Counter()
    for pkg, n in counts.items():
        groups["/".join(pkg.split("/")[:depth])] += n
    return groups


def _significant(groups: Counter, min_symbols: int) -> list[tuple[str, int]]:
    sig = [(p, n) for p, n in groups.items() if n >= min_symbols and p]
    return sorted(sig, key=lambda x: (-x[1], x[0]))


def _all_at_depth(counts: dict[str, int], depth: int) -> list[tuple[str, int]]:
    groups = _groups_at_depth(counts, depth)
    return sorted(((p, n) for p, n in groups.items() if p), key=lambda x: (-x[1], x[0]))


def choose_groups(counts: dict[str, int], *, min_symbols: int = MIN_SYMBOLS,
                  max_pages: int = MAX_PAGES) -> list[tuple[str, int]]:
    """Shallowest depth that splits the repo into >1 group; deepest tried wins otherwise.

    A repo too small for any group to clear ``min_symbols`` still gets its top-level dirs — a
    thin taxonomy beats the zero-pages dead end.
    """
    best: list[tuple[str, int]] = []
    for depth in range(1, MAX_DEPTH + 1):
        sig = _significant(_groups_at_depth(counts, depth), min_symbols)
        if len(sig) > len(best):
            best = sig
        if len(sig) > 1:
            break
    return (best or _all_at_depth(counts, 1))[:max_pages]


def roots(counts: dict[str, int], *, min_symbols: int = MIN_SYMBOLS) -> list[str]:
    """Top-level directories worth naming in the overview page's include list."""
    sig = _significant(_groups_at_depth(counts, 1), min_symbols)
    return [p for p, _ in (sig or _all_at_depth(counts, 1))]


def _slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def _leaf(prefix: str) -> str:
    return prefix.rstrip("/").split("/")[-1]


def _title_for(prefix: str) -> str:
    words = [w for w in re.split(r"[-_.]+", _leaf(prefix)) if w]
    return " ".join(w.capitalize() for w in words) or _leaf(prefix)


def _domain_owner(conn: sqlite3.Connection, prefixes: list[str]) -> dict[str, str]:
    """kind -> the prefix holding the most domain nodes of that kind."""
    per_kind: dict[str, Counter] = {}
    for r in conn.execute("SELECT kind, file_path FROM domain_nodes"):
        path = r["file_path"] or ""
        for p in prefixes:
            if path == p or path.startswith(p + "/"):
                per_kind.setdefault(r["kind"], Counter())[p] += 1
    return {kind: c.most_common(1)[0][0] for kind, c in per_kind.items() if c}


def has_tests(conn: sqlite3.Connection) -> list[str]:
    """Test package prefixes present in the graph, most-populated first."""
    rows = conn.execute(
        "SELECT package, COUNT(*) n FROM symbols WHERE package IS NOT NULL AND package != '' "
        "GROUP BY package ORDER BY n DESC")
    return [r["package"] for r in rows if is_test_path(r["package"])]


def propose(conn: sqlite3.Connection) -> list[dict]:
    """Build the starter page list. Empty when the graph has no packages (index not run)."""
    counts = package_counts(conn)
    if not counts:
        return []

    groups = choose_groups(counts)
    if not groups:
        return []

    top = roots(counts)
    owner = _domain_owner(conn, [p for p, _ in groups])

    pages: list[dict] = []
    order = 1
    if top:
        pages.append({"slug": "overview", "title": "Overview", "order": order, "include": list(top)})
        order += 1

    taken = {"overview"}
    for prefix, _ in groups:
        if len(groups) == 1 and prefix in top:
            continue                       # the overview page already covers it
        slug = _slugify(_leaf(prefix)) or f"page-{order}"
        while slug in taken:
            slug = f"{slug}-{order}"
        taken.add(slug)
        page = {"slug": slug, "title": _title_for(prefix), "order": order, "include": [prefix]}
        kinds = sorted(k for k, p in owner.items() if p == prefix)
        if kinds:
            page["domain"] = kinds
        pages.append(page)
        order += 1

    tests = has_tests(conn)
    if tests:
        pages.append({"slug": "testing", "title": "Testing", "order": order,
                      "keep_tests": True, "include": tests[:4]})
    return pages


def render_yaml(pages: list[dict]) -> str:
    """Hand-rendered so the generated file keeps its explanatory header and key order."""
    root = C.load().repo_root.name
    out = [
        f"# codewiki page taxonomy for {root}, generated by `codewiki init`.",
        "#",
        "# `include` entries are package path prefixes taken from the indexed code graph.",
        "# Edit freely: rename pages, merge or split groups, add `domain:` kinds. Re-run",
        "# `codewiki assemble --writer` after any change.",
        "",
        "pages:",
    ]
    for i, p in enumerate(pages):
        if i:
            out.append("")
        out.append(f"  - slug: {p['slug']}")
        out.append(f"    title: {p['title']}")
        out.append(f"    order: {p['order']}")
        if p.get("keep_tests"):
            out.append("    keep_tests: true")
        if p.get("domain"):
            out.append(f"    domain: [{', '.join(p['domain'])}]")
        out.append("    include:")
        for inc in p["include"]:
            out.append(f"      - {inc}")
    return "\n".join(out) + "\n"
