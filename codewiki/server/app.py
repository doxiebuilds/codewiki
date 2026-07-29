"""
app.py — reference FastAPI serving layer for a generated wiki.

A minimal, distilled example of how to expose ``codewiki/server/reader.py`` over HTTP for the
bundled ``codewiki/viewer/index.html`` (or any other client). Requires the ``server`` extra
(``pip install codegraph-wiki[server]``) for FastAPI/uvicorn; the core ``codewiki`` package has
no web-framework dependency.

Run it from inside the repo you've indexed:

    codewiki update                          # generate docs/wiki/*.md once
    uvicorn codewiki.server.app:app --reload # then serve it

Routes mirror ``docs/OUTPUT_CONTRACT.md``:
    GET  /api/wiki/manifest
    GET  /api/wiki/page/{slug}
    GET  /api/wiki/search?q=...
    POST /api/wiki/refresh
    GET  /api/wiki/refresh/status
    POST /api/wiki/refresh/stop
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from codewiki.llm import lmstudio_up
from codewiki.paths import REPO_ROOT
from codewiki.server import reader

logger = logging.getLogger(__name__)

app = FastAPI(title="codewiki reference server")

STATUS_PATH = reader.refresh_status_path()
LOG_PATH = REPO_ROOT / "docs" / ".codewiki_state" / "refresh.log"


def _pid_is_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError, OSError):
        return False


def _write_status(payload: dict[str, Any]) -> None:
    STATUS_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATUS_PATH.parent / (STATUS_PATH.name + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    os.replace(tmp, STATUS_PATH)


@app.get("/api/wiki/manifest")
async def get_manifest():
    try:
        return await asyncio.to_thread(reader.load_manifest)
    except Exception as exc:  # never 500 the docs tab
        return JSONResponse(status_code=200, content={
            "available": False, "pages": [], "page_count": 0, "error": str(exc)})


@app.get("/api/wiki/page/{slug}")
async def get_page(slug: str):
    try:
        page = await asyncio.to_thread(reader.read_page, slug)
    except Exception as exc:
        return JSONResponse(status_code=500, content={"error": str(exc)})
    if page is None:
        return JSONResponse(status_code=404, content={"error": f"Unknown wiki page '{slug}'"})
    return page


@app.get("/api/wiki/search")
async def search(q: str = "", limit: int = 20):
    try:
        results = await asyncio.to_thread(reader.search, q, limit)
    except Exception as exc:
        return JSONResponse(status_code=200, content={"query": q, "results": [], "error": str(exc)})
    return {"query": q, "results": results}


@app.post("/api/wiki/refresh")
async def refresh():
    """Launch a detached `codewiki update` rebuild. 503 = LLM server down, 409 = already running."""
    if not await asyncio.to_thread(lmstudio_up):
        return JSONResponse(status_code=503, content={
            "error": "No local LLM server reachable — start LM Studio (or your configured "
                     "CODEWIKI_LLM_BASE_URL server) and load a model."})

    status = await asyncio.to_thread(reader.load_refresh_status)
    if status.get("state") == "running":
        pid = status.get("pid")
        if pid and _pid_is_running(pid):
            return JSONResponse(status_code=409,
                                content={"error": "Wiki refresh already running.", "pid": pid})

    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    started_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    run_id = uuid.uuid4().hex[:12]
    # `-m codewiki.build` rather than a hand-computed script path: works the same whether
    # codewiki is editable-installed, wheel-installed, or vendored somewhere unusual.
    command = [sys.executable, "-m", "codewiki.build", "update", "--status-file", str(STATUS_PATH)]
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as log_handle:
            proc = subprocess.Popen(command, cwd=str(REPO_ROOT), stdout=log_handle,
                                    stderr=log_handle, start_new_session=True)
    except Exception:
        logger.exception("Failed to start wiki refresh")
        return JSONResponse(status_code=500,
                            content={"error": "Failed to start wiki refresh — check server logs."})

    # Stub status so a poller can start immediately; the child overwrites it with its own
    # progress (and run_id/pid) as soon as it boots.
    _write_status({"state": "running", "stage": "index", "pct": 0.0, "detail": "starting",
                   "counts": {"done": 0, "total": 0}, "run_id": run_id, "pid": proc.pid,
                   "started_at": started_at, "finished_at": None, "error": None})
    return {"status": "started", "pid": proc.pid, "started_at": started_at}


@app.get("/api/wiki/refresh/status")
async def refresh_status():
    status = await asyncio.to_thread(reader.load_refresh_status)
    is_running = False
    if status.get("state") == "running":
        pid = status.get("pid")
        if pid and _pid_is_running(pid):
            is_running = True
        else:
            status["state"] = "stale"
    status["is_running"] = is_running
    return status


@app.post("/api/wiki/refresh/stop")
async def refresh_stop():
    """SIGTERM (then SIGKILL) the refresh process group; mark the run as stopped."""
    status = await asyncio.to_thread(reader.load_refresh_status)
    if status.get("state") != "running":
        return JSONResponse(status_code=400, content={"error": "Wiki refresh is not running."})
    pid = status.get("pid")
    if not pid:
        return JSONResponse(status_code=500, content={"error": "No PID found for running process."})

    try:
        try:
            os.killpg(os.getpgid(pid), signal.SIGTERM)
        except ProcessLookupError:
            raise
        except Exception:
            os.kill(pid, signal.SIGTERM)
        await asyncio.sleep(0.5)
        if _pid_is_running(pid):
            try:
                os.killpg(os.getpgid(pid), signal.SIGKILL)
            except ProcessLookupError:
                pass
            except Exception:
                os.kill(pid, signal.SIGKILL)
        try:
            os.waitpid(pid, os.WNOHANG)
        except Exception:
            pass
    except ProcessLookupError:
        pass  # already gone — still record the stop below
    except Exception:
        logger.exception("Failed to stop wiki refresh")
        return JSONResponse(status_code=500,
                            content={"error": "Failed to stop process — check server logs."})

    status["state"] = "error"
    status["error"] = "stopped by user"
    status["finished_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    _write_status(status)
    return {"status": "stopped", "pid": pid}
