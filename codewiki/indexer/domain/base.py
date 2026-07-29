"""
base.py — the DomainExtractor plugin protocol + registry (deterministic; no LLM).

File-level RAG can't reliably enumerate a system's moving parts — it can retrieve a plausible
chunk, not confirm every route or table actually exists. A domain extractor pulls exact, typed
nodes out of the code instead — HTTP routes, DB tables, message-bus
channels, ... — as ``domain_nodes`` rows the assembler renders into precise reference tables.
Seven broadly-useful, framework-shaped extractors ship built-in (``builtin.py``); write your own
for anything repo-specific (a service registry read from your own config format, feature flags,
...) and ``register`` it — see ``examples/plugins/`` for a worked example.

All extractors are best-effort and idempotent; each kind is replaced wholesale on every run. A
failing extractor never aborts the index — it just contributes zero rows for its kind.
"""

from __future__ import annotations

import sqlite3
from typing import Callable

DomainRow = dict  # {"id", "kind", "name", "detail", "file_path", "line"}
DomainExtractor = Callable[[sqlite3.Connection], list[DomainRow]]

_REGISTRY: dict[str, DomainExtractor] = {}
_ORDER: list[str] = []


def register(kind: str, fn: DomainExtractor, *, after: str | None = None) -> None:
    """Register an extractor under `kind`.

    `after`, if given and already registered, places this extractor's pass right after it in
    `index_domain`'s run order — for extractors that read another kind's freshly-written rows
    within the same index pass (e.g. `api_call` matching against `route`).
    """
    _REGISTRY[kind] = fn
    if kind in _ORDER:
        _ORDER.remove(kind)
    if after and after in _ORDER:
        _ORDER.insert(_ORDER.index(after) + 1, kind)
    else:
        _ORDER.append(kind)


def unregister(kind: str) -> None:
    _REGISTRY.pop(kind, None)
    if kind in _ORDER:
        _ORDER.remove(kind)


def registered() -> list[str]:
    """Registered kinds, in the order `index_domain` runs them."""
    return list(_ORDER)


def index_domain(conn: sqlite3.Connection) -> dict[str, int]:
    from codewiki.store import db

    counts: dict[str, int] = {}
    for kind in _ORDER:
        fn = _REGISTRY[kind]
        try:
            rows = fn(conn)
        except Exception as exc:  # never let one fuzzy extractor abort the index
            print(f"  domain extractor {kind} failed: {exc}")
            rows = []
        db.replace_domain_nodes(conn, kind, rows)
        counts[kind] = len(rows)
    return counts
