"""
progress.py — machine-readable refresh status for a "Refresh" button in a consuming UI.

STDLIB ONLY and null-object by default: codewiki must keep running standalone (`codewiki update`)
with zero coupling to any specific consumer, so the reporter only writes when constructed with a
path (a caller passes ``--status-file``; plain CLI runs pass nothing and every method no-ops).
Writes are atomic (temp + ``os.replace``) and throttled, except state/stage changes which always
flush — a UI can poll this file every ~2.5s (see ``docs/OUTPUT_CONTRACT.md``).

Overall percentage is stage-windowed: index 0-5%, summarize 5-70%, pages 70-100% — summarize
dominates on backfills, and the raw counts ride along so a slow-crawling bar stays honest.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

STAGE_WINDOWS: dict[str, tuple[float, float]] = {
    "index": (0.0, 5.0),
    "summarize": (5.0, 70.0),
    "pages": (70.0, 100.0),
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class StatusReporter:
    """Writes the refresh status JSON; a no-op when constructed with ``path=None``."""

    def __init__(self, path: Path | str | None, run_id: str | None = None,
                 min_write_interval: float = 0.5) -> None:
        self.path = Path(path) if path else None
        self.run_id = run_id or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        self.min_write_interval = min_write_interval
        self._last_write = 0.0
        self._state: dict = {
            "state": "running", "stage": "index", "pct": 0.0, "detail": "",
            "counts": {"done": 0, "total": 0}, "run_id": self.run_id, "pid": os.getpid(),
            "started_at": None, "finished_at": None, "error": None,
        }

    @property
    def enabled(self) -> bool:
        return self.path is not None

    # ------------------------------------------------------------------ lifecycle
    def start(self, detail: str = "") -> None:
        self._state.update(state="running", stage="index", pct=0.0, detail=detail,
                           started_at=_now(), finished_at=None, error=None)
        self._write(force=True)

    def stage(self, name: str, total: int | None = None, detail: str = "") -> None:
        lo, _ = STAGE_WINDOWS.get(name, (self._state["pct"], 100.0))
        self._state.update(stage=name, pct=lo, detail=detail,
                           counts={"done": 0, "total": int(total or 0)})
        self._write(force=True)

    def tick(self, done: int, total: int, detail: str = "") -> None:
        lo, hi = STAGE_WINDOWS.get(self._state["stage"], (0.0, 100.0))
        frac = (done / total) if total > 0 else 1.0
        pct = min(hi, max(lo, lo + frac * (hi - lo)))
        self._state.update(pct=round(pct, 1), counts={"done": int(done), "total": int(total)})
        if detail:
            self._state["detail"] = detail
        self._write()

    def done(self, detail: str = "") -> None:
        self._state.update(state="done", pct=100.0, detail=detail, finished_at=_now())
        self._write(force=True)

    def error(self, message: str) -> None:
        self._state.update(state="error", error=message, finished_at=_now())
        self._write(force=True)

    # ------------------------------------------------------------------ io
    def _write(self, force: bool = False) -> None:
        if self.path is None:
            return
        now = time.monotonic()
        if not force and now - self._last_write < self.min_write_interval:
            return
        self._last_write = now
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(self._state, ensure_ascii=False) + "\n", encoding="utf-8")
        os.replace(tmp, self.path)
