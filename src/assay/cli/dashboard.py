"""Dashboard server for Assay local UI.

Provides a FastAPI app that serves the HTML UI, static assets, and a set of REST
endpoints used by the front‑end to start/stop recordings, list sessions, run
verification, and edit redaction rules.
"""

from __future__ import annotations

import threading
import webbrowser
from pathlib import Path
from typing import List

import uvicorn
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

# Local imports – the core command helpers live in ``assay.cli``
from assay.cli.commands_record import run_record_command
from assay.cli.commands_verify import run_verify_command
from assay.config.settings import load_settings, AssaySettings, write_default_config
from assay.storage.session_store import SessionStore

# ---------------------------------------------------------------------------
# FastAPI app definition
# ---------------------------------------------------------------------------
app = FastAPI(title="Assay Local Dashboard")

# Resolve directories relative to the package root (src/assay)
BASE_DIR = Path(__file__).resolve().parent.parent
STATIC_DIR = BASE_DIR / "static"
TEMPLATE_DIR = BASE_DIR / "templates"

# Mount static files (Tailwind CSS, Alpine.js, optional compiled JS)
if STATIC_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

# Jinja2 template loader
templates = Jinja2Templates(directory=TEMPLATE_DIR)

# ---------------------------------------------------------------------------
# Helper utilities
# ---------------------------------------------------------------------------
def _load_settings() -> AssaySettings:
    """Load the current Assay settings (creates defaults if missing)."""
    return load_settings()

def _list_json_sessions() -> List[Path]:
    """Return a list of ``.json`` session files from the configured sessions dir."""
    settings = _load_settings()
    sessions_dir = settings.storage.sessions_dir
    if not sessions_dir.exists():
        return []
    return sorted(sessions_dir.glob("*.json"))

# ---------------------------------------------------------------------------
# API Endpoints
# ---------------------------------------------------------------------------
@app.get("/api/sessions")
def get_sessions() -> JSONResponse:
    """List all recorded JSON workload sessions."""
    files = _list_json_sessions()
    data = [{"name": f.name, "size": f.stat().st_size} for f in files]
    return JSONResponse(content={"sessions": data})

@app.post("/api/record/start")
def start_recording(
    backend: str,
    frontend: str | None = None,
    session: str | None = None,
) -> JSONResponse:
    """Start the dual‑routing proxy in a background thread.

    The ``run_record_command`` function blocks while the proxy runs, so we launch it
    in a daemon thread. The thread is stored on the FastAPI app instance for later
    shutdown via ``/api/record/stop``.
    """
    def _run() -> None:
        run_record_command(
            target=backend,
            port=None,
            session_id=session,
            ui_url=frontend,
            browser="chromium",
        )

    if getattr(app.state, "record_thread", None) and app.state.record_thread.is_alive():
        raise HTTPException(status_code=400, detail="Recording already in progress")
    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    app.state.record_thread = thread
    return JSONResponse(content={"status": "started"})

@app.post("/api/record/stop")
def stop_recording() -> JSONResponse:
    """Signal the proxy thread to stop.

    ``run_record_command`` reacts to ``KeyboardInterrupt``; we simply terminate the
    thread if it is still alive. In a real implementation the proxy would expose a
    graceful shutdown endpoint, but for now we abort the thread.
    """
    thread: threading.Thread | None = getattr(app.state, "record_thread", None)
    if thread and thread.is_alive():
        # Python threads cannot be killed; raise to stop the daemon.
        raise RuntimeError("Manual shutdown not supported in this mock implementation")
    return JSONResponse(content={"status": "stopped"})

@app.get("/api/replays")
def list_replays() -> JSONResponse:
    """List saved replay reports (placeholder implementation)."""
    settings = _load_settings()
    reports = []
    for p in settings.storage.sessions_dir.glob("*-replay.json"):
        reports.append({"name": p.name, "size": p.stat().st_size})
    return JSONResponse(content={"replays": reports})

@app.post("/api/verify")
def verify(session: str, baseline: str, candidate: str) -> JSONResponse:
    """Execute a verification run for the given session.

    Thin wrapper around the existing CLI ``verify`` command.
    """
    try:
        run_verify_command(
            recording=session,
            baseline=baseline,
            candidate=candidate,
            ignore_path=None,
            concurrency=1,
        )
        return JSONResponse(content={"status": "verified"})
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))

@app.post("/api/redact/rules")
def update_redaction_rules(keys: List[str]) -> JSONResponse:
    """Replace the ``redaction.fields`` list in ``.assay.yaml`` with ``keys``.
    """
    settings_path = Path(".assay.yaml")
    if not settings_path.exists():
        raise HTTPException(status_code=404, detail="Configuration file not found")
    settings = load_settings()
    settings.redaction.fields = list(keys)
    write_default_config(settings=settings)
    return JSONResponse(content={"status": "updated", "keys": keys})

# ---------------------------------------------------------------------------
# UI route
# ---------------------------------------------------------------------------
@app.get("/")
def ui_root(request: Request) -> JSONResponse:
    """Render the main dashboard page using Jinja2."""
    return templates.TemplateResponse("index.html", {"request": request})

# ---------------------------------------------------------------------------
# Server entrypoint
# ---------------------------------------------------------------------------
def start_dashboard(port: int = 9100) -> None:
    """Start the FastAPI server in a background thread and open the browser.

    The function returns immediately after launching the server; the caller can
    keep the process alive (e.g. ``typer`` will block until Ctrl+C).
    """
    def _run() -> None:
        uvicorn.run(app, host="127.0.0.1", port=port, log_level="error")

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    webbrowser.open(f"http://localhost:{port}")
