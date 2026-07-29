"""
services_tiers.py — example DomainExtractor plugin: a service registry read from your own
config format.

This mirrors a common pattern: a repo that declares every long-running service in one YAML file
(name, deployment tier, description) gets that turned into a "Services & Tiers"
reference table on its wiki pages, with zero LLM involvement — same mechanism as the built-in
`route`/`db_table`/etc. extractors in `codewiki/indexer/domain/builtin.py`, just pointed at your
own config file and schema instead of a generic language convention.

Usage: import this module once (e.g. at the top of your own `codewiki.toml`-adjacent script, or
via a `sitecustomize.py` / pytest `conftest.py` for the repo you're documenting) before running
`codewiki index`:

    from examples.plugins.services_tiers import register_services_extractor
    register_services_extractor()

Expects a ``services.yaml`` at the repo root shaped like:

    services:
      api:
        tier: "1"
        description: "Public HTTP API"
      worker:
        tier: "2"
        description: "Background job runner"

``assembly/render.py`` already knows how to render a ``kind="service"`` domain table (see its
``_domain_table`` function) — this plugin only needs to produce the rows.
"""

from __future__ import annotations

import json
import sqlite3

import yaml

from codewiki.indexer.domain import register
from codewiki.paths import PROJECT_ROOT, rel_to_repo


def extract_services(conn: sqlite3.Connection) -> list[dict]:
    path = PROJECT_ROOT / "services.yaml"
    if not path.exists():
        return []
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    rows = []
    for name, spec in (data.get("services") or {}).items():
        spec = spec or {}
        rows.append({
            "id": f"service::{name}", "kind": "service", "name": name,
            "detail": json.dumps({"tier": spec.get("tier"),
                                  "description": spec.get("description", "")}),
            "file_path": rel_to_repo(path), "line": None,
        })
    return rows


def register_services_extractor() -> None:
    register("service", extract_services)
