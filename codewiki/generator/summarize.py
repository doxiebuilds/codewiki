"""
summarize.py — hierarchical, hash-gated summaries (the ONLY LLM stage).

We send a single chat completion per node with the pre-assembled structured context from
``context.py`` rather than letting a model roam the whole repo (huge context, whole-page regen).
Each node's summary is stored keyed by the exact hash it was generated for, so re-running only
touches nodes whose hash moved: one function edit → that function + its class + its module +
its package.

The LLM call is injectable (``chat_fn``) so tests prove the hash-gating without a live model.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from dataclasses import dataclass, field

from codewiki.llm import DEFAULT_MODEL, lmstudio_chat as _llm_chat
from codewiki.generator import context as ctx
from codewiki.store import db

_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)

SYSTEM_PROMPT = (
    "You are a precise code documentation summarizer for a software system. You are given "
    "PRE-EXTRACTED context about ONE code node (its signature, docstring, callers, callees, and "
    "either its source or its members' summaries). Summarize ONLY from that context — do not "
    "invent behavior, file paths, or symbols not present. Be concrete and technical. "
    "Never overstate the node's scope beyond what the context shows. Name a design pattern or "
    "algorithm (token bucket, singleton, LRU, …) ONLY if the code or context explicitly shows "
    "it — describe the actual mechanism otherwise. If the node is trivial or empty (a package "
    "marker, re-export stub, or bare __init__), say so in ONE short sentence — never pad "
    "trivial nodes with filler prose. "
    "Respond with a single JSON object and nothing else."
)

# Folded into every staleness hash: bumping it (after a prompt change) or swapping the model
# invalidates ALL stored summaries — a deliberate, visible cost. Without it, prompt improvements
# would silently never retrigger already-summarized nodes.
#
# Calibration lessons baked into the current prompt (bump this if you change any of it): trivial
# nodes (a package marker, a re-export stub) get one short sentence, never padded to look
# substantial; summaries state the caller-facing role — why the node exists — rather than
# restating its signature; a design pattern or algorithm is named only if the code actually
# shows it, never inferred from a plausible-looking shape; small classes get their real source
# in context, not just their members' summaries, because summaries-of-summaries lose the actual
# mechanism; callees are enriched with resolved targets where the graph has them (e.g.
# "handle_request → server.py:App.handle_request"); oversized spans are head/tail-elided at line
# boundaries instead of hard-truncated mid-body.
SUMMARY_PROMPT_VERSION = "s5"

_LEAF_KINDS = {"function", "method"}
_ROLLUP_ORDER = ["function", "method", "class", "module"]


def target_hash(base_hash: str, model: str) -> str:
    """Staleness key = content/rollup hash + prompt version + model."""
    joined = f"{base_hash}|{SUMMARY_PROMPT_VERSION}|{model}"
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


@dataclass
class SummaryStats:
    generated: int = 0
    skipped: int = 0
    failed: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    by_kind: dict[str, int] = field(default_factory=dict)


# ------------------------------------------------------------------ LLM transport
def lmstudio_chat(prompt: str, *, model: str = DEFAULT_MODEL, system: str = SYSTEM_PROMPT,
                  max_tokens: int = 512, timeout: int = 120) -> tuple[str, dict]:
    """One chat completion against the local OpenAI-compatible server. Returns (text, usage)."""
    return _llm_chat(prompt, model=model, system=system, max_tokens=max_tokens, timeout=timeout)


# ------------------------------------------------------------------ prompt + parsing
def _leaf_prompt(context_block: str) -> str:
    return (
        "Summarize this code node. Return JSON with keys: purpose (1 sentence), inputs, outputs, "
        "side_effects, summary (2-4 sentences: what it does, why it exists, and the role it "
        "plays for its callers — use CALLED BY and DECORATORS as evidence; do not merely "
        "restate the signature).\n\n"
        f"{context_block}"
    )


def _container_prompt(kind: str, context_block: str) -> str:
    what = {"class": "class/type", "module": "module (file)"}.get(kind, kind)
    return (
        f"Summarize this {what} from its members' summaries. Return JSON with keys: purpose "
        "(1 sentence), responsibilities (list), summary (3-5 sentences on its role in the "
        "system, main workflow, and how its parts fit together; one short sentence if it is "
        "trivial).\n\n"
        f"{context_block}"
    )


def _parse_json(text: str) -> dict:
    text = _THINK_RE.sub("", text or "").strip()   # defensive: drop any inline <think> block
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except json.JSONDecodeError:
            pass
    return {"summary": " ".join(text.split())[:800]}


def _is_valid_summary(d: dict) -> bool:
    """A usable summary must carry non-empty prose — guards against truncated/empty model output
    being stored as 'done' (which would freeze a blank page in forever via hash-gating)."""
    return bool((d.get("summary") or d.get("purpose") or "").strip())


def count_stale_nodes(conn: sqlite3.Connection, model: str,
                      kinds: set[str] | None = None) -> int:
    """Nodes whose stored summary key differs from the current versioned target.

    The denominator for refresh progress bars and the `status` report — computed in Python
    because the target folds SUMMARY_PROMPT_VERSION + model into the hash.
    """
    kinds = kinds or set(_ROLLUP_ORDER)
    stored = {r["node_id"]: r["hash"] for r in conn.execute("SELECT node_id, hash FROM summaries")}
    stale = 0
    qmarks = ",".join("?" * len(kinds))
    for s in conn.execute(f"SELECT id, kind, content_hash, rollup_hash FROM symbols "
                          f"WHERE kind IN ({qmarks})", tuple(kinds)):
        base = s["content_hash"] if s["kind"] in _LEAF_KINDS else s["rollup_hash"]
        if stored.get(s["id"]) != target_hash(base, model):
            stale += 1
    if "package" in kinds or kinds == set(_ROLLUP_ORDER):
        for r in conn.execute("SELECT DISTINCT package FROM symbols ORDER BY package"):
            node_id = f"pkg::{r['package']}"
            if stored.get(node_id) != target_hash(package_rollup_hash(conn, r["package"]), model):
                stale += 1
    return stale


def package_rollup_hash(conn: sqlite3.Connection, package: str) -> str:
    rows = conn.execute(
        "SELECT rollup_hash FROM symbols WHERE kind='module' AND package=? ORDER BY rollup_hash",
        (package,)).fetchall()
    joined = f"pkg:{package}|" + "".join(r["rollup_hash"] for r in rows)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


# ------------------------------------------------------------------ driver
def summarize_all(conn: sqlite3.Connection, *, chat_fn=lmstudio_chat, model: str = DEFAULT_MODEL,
                  only_stale: bool = True, limit: int | None = None,
                  kinds: set[str] | None = None, verbose: bool = False,
                  commit_every: int = 1, concurrency: int = 1,
                  on_progress=None) -> SummaryStats:
    """Summarize every stale node bottom-up (function→method→class→module→package).

    Each summary is committed as soon as it lands (``commit_every=1``) — crash-safe, resumable,
    and visible live to ``codewiki-status``. ``concurrency`` overlaps the LLM HTTP calls *within*
    a level (the levels are a dependency barrier: a class needs its methods first), while ALL
    SQLite access stays on the calling thread (sqlite connections aren't thread-safe).
    """
    stats = SummaryStats()
    kinds = kinds or set(_ROLLUP_ORDER)
    progress_total = 0
    progress_done = 0
    if on_progress is not None:
        progress_total = count_stale_nodes(conn, model, kinds) if only_stale else 0
        if limit is not None:
            progress_total = min(progress_total, limit) if progress_total else limit

    def _tick() -> None:
        nonlocal progress_done
        if on_progress is None:
            return
        progress_done += 1
        on_progress(progress_done, max(progress_total, progress_done))

    def _remaining() -> int | None:
        return None if limit is None else max(0, limit - stats.generated)

    def _call(prompt: str):
        try:
            return chat_fn(prompt, model=model)
        except (urllib.error.URLError, OSError, KeyError, TimeoutError) as exc:
            if verbose:
                print(f"  LLM call failed: {exc}")
            return None

    def _write(node_id: str, node_kind: str, target_hash: str, result) -> None:
        _tick()                                     # attempted node (written or failed)
        if result is None:
            stats.failed += 1
            return
        text, usage = result
        summary = _parse_json(text)
        if not _is_valid_summary(summary):
            stats.failed += 1   # leave it stale so it's retried, don't store an empty summary
            if verbose:
                print(f"  empty/invalid summary for {node_id} (finish likely truncated) — skipped")
            return
        db.upsert_summary(conn, node_id=node_id,
                          node_kind="package" if node_kind == "package" else "symbol",
                          hash_=target_hash, summary=summary, model=model,
                          tokens_in=usage.get("prompt_tokens", 0),
                          tokens_out=usage.get("completion_tokens", 0),
                          generated_at=datetime.now(timezone.utc).isoformat())
        stats.generated += 1
        stats.by_kind[node_kind] = stats.by_kind.get(node_kind, 0) + 1
        stats.tokens_in += usage.get("prompt_tokens", 0)
        stats.tokens_out += usage.get("completion_tokens", 0)
        if commit_every and stats.generated % commit_every == 0:
            conn.commit()   # persist immediately: crash-safe + live in `codewiki-status`
        if verbose and stats.generated % 25 == 0:
            print(f"  …{stats.generated} generated, {stats.skipped} skipped")

    def _run_level(work: list[tuple[str, str, str, str]]) -> None:
        """work = [(node_id, node_kind, target_hash, prompt), …] — one dependency level."""
        if concurrency <= 1 or len(work) <= 1:
            for node_id, node_kind, target, prompt in work:
                _write(node_id, node_kind, target, _call(prompt))
            return
        # Overlap the HTTP calls; writes happen here on the main thread as futures complete.
        with ThreadPoolExecutor(max_workers=concurrency) as ex:
            futs = {ex.submit(_call, prompt): (nid, nk, th) for nid, nk, th, prompt in work}
            for fut in as_completed(futs):
                nid, nk, th = futs[fut]
                _write(nid, nk, th, fut.result())

    def _build_symbol_work(kind: str) -> list[tuple[str, str, str, str]]:
        rows = conn.execute("SELECT * FROM symbols WHERE kind=? ORDER BY file_path, start_line",
                            (kind,)).fetchall()
        work: list[tuple[str, str, str, str]] = []
        for sym in rows:
            node_id = sym["id"]
            target = target_hash(
                sym["content_hash"] if kind in _LEAF_KINDS else sym["rollup_hash"], model)
            if only_stale and db.summary_hash(conn, node_id) == target:
                stats.skipped += 1
                continue
            if kind in _LEAF_KINDS:
                prompt = _leaf_prompt(ctx.leaf_context(conn, sym))
            else:
                prompt = _container_prompt(kind, ctx.container_context(conn, sym))
            work.append((node_id, kind, target, prompt))
            rem = _remaining()
            if rem is not None and len(work) >= rem:
                break
        return work

    # symbol levels, in dependency order (barrier between each)
    for kind in _ROLLUP_ORDER:
        if kind not in kinds:
            continue
        _run_level(_build_symbol_work(kind))
        if _remaining() == 0:
            conn.commit()
            return stats

    # package rollups (summary-of-module-summaries), last
    if "package" in kinds or kinds == set(_ROLLUP_ORDER):
        work: list[tuple[str, str, str, str]] = []
        for pkg in [r["package"] for r in conn.execute(
                "SELECT DISTINCT package FROM symbols ORDER BY package")]:
            mods = ctx.package_module_summaries(conn, pkg)
            if not mods:
                continue
            node_id, target = f"pkg::{pkg}", target_hash(package_rollup_hash(conn, pkg), model)
            if only_stale and db.summary_hash(conn, node_id) == target:
                stats.skipped += 1
                continue
            block = f"PACKAGE: {pkg}\nMODULES:\n" + "\n".join(f"  - {n}: {s}" for n, s in mods)
            work.append((node_id, "package", target, _container_prompt("module", block)))
            rem = _remaining()
            if rem is not None and len(work) >= rem:
                break
        _run_level(work)

    conn.commit()
    return stats
