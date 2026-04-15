"""Server-Sent Events log streaming route.

GET /api/logs/{vm_name}  — SSE stream of the VM's migration log file.

Each event is a JSON object:
  {"line": "...", "level": "INFO", "timestamp": "2026-04-09T10:00:00"}
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

logger = logging.getLogger("vmigrate.web.logs")

router = APIRouter(tags=["logs"])

# Matches log lines like: 2026-04-09 10:00:00,123 INFO  vmigrate... message
_LOG_RE = re.compile(
    r"(?P<ts>\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}[^,]*),?\d*\s+"
    r"(?P<level>DEBUG|INFO|WARNING|ERROR|CRITICAL)\s+"
    r"(?P<rest>.*)"
)

_WORK_DIR = Path("/var/lib/vmigrate")


def _find_log_file(vm_name: str) -> Path | None:
    """Return the most recent log file for a given VM name."""
    log_dir = _WORK_DIR / "logs"
    if not log_dir.exists():
        return None
    # Pattern: {vm_name}_{timestamp}.log
    safe_name = vm_name.replace(" ", "_")
    candidates = sorted(
        log_dir.glob(f"{safe_name}_*.log"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return candidates[0] if candidates else None


def _parse_log_line(raw: str) -> dict:
    """Parse a raw log line into a structured dict."""
    line = raw.rstrip("\n")
    m = _LOG_RE.match(line)
    if m:
        return {
            "line": m.group("rest"),
            "level": m.group("level"),
            "timestamp": m.group("ts"),
        }
    return {"line": line, "level": "INFO", "timestamp": ""}


async def _sse_log_generator(vm_name: str, request: Request):
    """Async generator that yields SSE-formatted log lines."""
    # Send a heartbeat comment immediately so the browser registers the connection
    yield ": connected\n\n"

    log_path = _find_log_file(vm_name)
    file_handle = None
    position = 0

    try:
        # Wait up to 30 seconds for the log file to appear
        for _ in range(60):
            if await request.is_disconnected():
                return
            log_path = _find_log_file(vm_name)
            if log_path:
                break
            await asyncio.sleep(0.5)

        if not log_path:
            data = json.dumps({"line": f"No log file found for VM '{vm_name}'.",
                               "level": "WARNING", "timestamp": ""})
            yield f"data: {data}\n\n"
            return

        file_handle = open(log_path, "r", encoding="utf-8", errors="replace")

        while True:
            if await request.is_disconnected():
                break

            # Re-check for a newer log file (retry creates a new one)
            newer = _find_log_file(vm_name)
            if newer and newer != log_path:
                file_handle.close()
                log_path = newer
                file_handle = open(log_path, "r", encoding="utf-8", errors="replace")
                position = 0

            file_handle.seek(position)
            new_lines = file_handle.readlines()
            position = file_handle.tell()

            for raw_line in new_lines:
                if raw_line.strip():
                    parsed = _parse_log_line(raw_line)
                    yield f"data: {json.dumps(parsed)}\n\n"

            if not new_lines:
                # Send keepalive heartbeat every 2 seconds of silence
                yield ": heartbeat\n\n"
                await asyncio.sleep(2)
            else:
                await asyncio.sleep(0.1)

    except asyncio.CancelledError:
        pass
    except Exception as exc:
        logger.warning("SSE log stream error for VM '%s': %s", vm_name, exc)
        data = json.dumps({"line": f"Log stream error: {exc}", "level": "ERROR", "timestamp": ""})
        yield f"data: {data}\n\n"
    finally:
        if file_handle:
            try:
                file_handle.close()
            except Exception:
                pass


@router.get("/logs/{vm_name}")
async def stream_logs(vm_name: str, request: Request) -> StreamingResponse:
    """Stream migration log lines for a VM as Server-Sent Events.

    The client should use the EventSource API::

        const es = new EventSource('/api/logs/my-vm');
        es.onmessage = e => {
            const {line, level, timestamp} = JSON.parse(e.data);
            ...
        };
    """
    return StreamingResponse(
        _sse_log_generator(vm_name, request),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",   # disable nginx buffering
        },
    )
