"""FastAPI application factory and session store for vmigrate Web UI.

Session store is an in-memory dict keyed by UUID session cookie.
Each session holds VMware/Proxmox connection params, client objects,
migration state references, and job futures.
"""

from __future__ import annotations

import threading
import time
import uuid
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

# ---------------------------------------------------------------------------
# Session store
# ---------------------------------------------------------------------------

_SESSION_TTL_SECONDS = 30 * 60  # 30 minutes
_SESSION_COOKIE = "vmigrate_session"

_sessions: dict[str, dict] = {}
_sessions_lock = threading.Lock()


def _now() -> float:
    return time.monotonic()


def get_session(session_id: str) -> dict:
    """Return the session dict for ``session_id``, or an empty dict."""
    with _sessions_lock:
        entry = _sessions.get(session_id)
        if entry is None:
            return {}
        entry["_accessed"] = _now()
        return entry["data"]


def set_session(session_id: str, data: dict) -> None:
    """Overwrite the session dict for ``session_id``."""
    with _sessions_lock:
        _sessions[session_id] = {"data": data, "_accessed": _now()}


def create_session() -> str:
    """Create a new session and return its ID."""
    sid = str(uuid.uuid4())
    with _sessions_lock:
        _sessions[sid] = {"data": {}, "_accessed": _now()}
    return sid


def purge_expired_sessions() -> None:
    """Remove sessions that have not been accessed within the TTL."""
    cutoff = _now() - _SESSION_TTL_SECONDS
    with _sessions_lock:
        expired = [sid for sid, entry in _sessions.items()
                   if entry["_accessed"] < cutoff]
        for sid in expired:
            _sessions.pop(sid, None)


def get_or_create_session(request: Request, response: Response) -> tuple[str, dict]:
    """Extract the session from the request cookie, creating one if absent.

    Returns ``(session_id, session_data)``.  Callers must call
    ``set_session(session_id, data)`` to persist changes and attach the
    cookie to ``response`` when a new session is created.
    """
    sid = request.cookies.get(_SESSION_COOKIE)
    if sid and sid in _sessions:
        data = get_session(sid)
        return sid, data
    sid = create_session()
    response.set_cookie(
        key=_SESSION_COOKIE,
        value=sid,
        httponly=True,
        samesite="lax",
        max_age=_SESSION_TTL_SECONDS,
    )
    return sid, {}


# ---------------------------------------------------------------------------
# Background TTL reaper (daemon thread)
# ---------------------------------------------------------------------------

def _reaper_loop() -> None:
    while True:
        time.sleep(300)  # check every 5 minutes
        try:
            purge_expired_sessions()
        except Exception:
            pass


_reaper = threading.Thread(target=_reaper_loop, daemon=True, name="session-reaper")
_reaper.start()


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------

_STATIC_DIR = Path(__file__).parent / "static"


def create_app(config_path: Optional[str] = None) -> FastAPI:
    """Create and configure the FastAPI application.

    Args:
        config_path: Optional path to a migration.yaml to pre-load.  Not
            required — users configure everything through the UI.

    Returns:
        Configured ``FastAPI`` instance ready to be served by uvicorn.
    """
    app = FastAPI(
        title="vmigrate Web UI",
        description="Web interface for migrating VMs from VMware vSphere to Proxmox VE",
        version="0.1.0",
    )

    # Store config_path on app state for routes that need it
    app.state.config_path = config_path

    # ----- API routers -----
    from vmigrate.web.routes.connections import router as connections_router
    from vmigrate.web.routes.inventory import router as inventory_router
    from vmigrate.web.routes.migration import router as migration_router
    from vmigrate.web.routes.logs import router as logs_router

    app.include_router(connections_router, prefix="/api")
    app.include_router(inventory_router, prefix="/api")
    app.include_router(migration_router, prefix="/api")
    app.include_router(logs_router, prefix="/api")

    # ----- Health check endpoint -----
    @app.get("/health", include_in_schema=False)
    async def health() -> dict:
        """Simple health check for container orchestration."""
        return {"status": "ok", "service": "vmigrate-web"}

    # ----- Static files -----
    if _STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

    # ----- SPA catch-all -----
    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(
            str(_STATIC_DIR / "index.html"),
            headers={"Cache-Control": "no-cache, no-store, must-revalidate"}
        )

    @app.get("/{full_path:path}", include_in_schema=False)
    async def spa_fallback(full_path: str) -> FileResponse:
        # Only return the SPA for non-API paths
        if full_path.startswith("api/"):
            return JSONResponse({"detail": "Not found"}, status_code=404)
        return FileResponse(
            str(_STATIC_DIR / "index.html"),
            headers={"Cache-Control": "no-cache, no-store, must-revalidate"}
        )

    return app
