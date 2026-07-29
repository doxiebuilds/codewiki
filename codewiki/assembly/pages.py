"""
pages.py — load the declarative page taxonomy (pages.yaml) into PageSpec objects.

Looks for ``pages.yaml`` at the target repo's root first; falls back to the generic
``pages.example.yaml`` bundled with codewiki when the target repo has none of its own.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

from codewiki import config as C

BUNDLED_EXAMPLE = Path(__file__).resolve().parent / "pages.example.yaml"


@dataclass
class PageSpec:
    slug: str
    title: str
    order: int
    include: list[str] = field(default_factory=list)      # package path prefixes
    domain: list[str] = field(default_factory=list)        # domain node kinds to render
    keep_tests: bool = False                               # rank test files normally (testing page)


def default_pages_path() -> Path:
    repo_pages = C.load().repo_root / "pages.yaml"
    return repo_pages if repo_pages.exists() else BUNDLED_EXAMPLE


def load_pages(path: Path | None = None) -> list[PageSpec]:
    path = path or default_pages_path()
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    specs = [
        PageSpec(slug=p["slug"], title=p["title"], order=int(p.get("order", i)),
                 include=list(p.get("include", [])), domain=list(p.get("domain", [])),
                 keep_tests=bool(p.get("keep_tests", False)))
        for i, p in enumerate(data.get("pages", []))
    ]
    return sorted(specs, key=lambda s: s.order)
