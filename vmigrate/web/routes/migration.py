"""Migration control routes.

POST /api/migrate/start      — start background migration job
GET  /api/migrate/status     — poll VM migration status
POST /api/migrate/retry/{vm} — resume failed VM
POST /api/migrate/rollback/{vm} — rollback VM
POST /api/migrate/stop       — mark pending VMs as cancelled
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Request, Response

from vmigrate.web.app import get_or_create_session, set_session
from vmigrate.web.models import (
    ActionResponse,
    ConfirmationRequest,
    ConfirmationResponse,
    ExecuteActionRequest,
    MigrationStartResponse,
    ResourceMapping,
    StartMigrationRequest,
    VMStatus,
)
from vmigrate.state import ORDERED_PHASES, Phase, PhaseStatus, StateDB

logger = logging.getLogger("vmigrate.web.migration")

router = APIRouter(tags=["migration"])

# ---------------------------------------------------------------------------
# Module-level job registry (independent of sessions — survives page refresh)
# ---------------------------------------------------------------------------

_executor = ThreadPoolExecutor(max_workers=8, thread_name_prefix="vmigrate-worker")
_jobs: dict[str, dict] = {}   # job_id → {futures, state_db, vm_names, work_dir}
_jobs_lock = threading.Lock()

# Confirmation token registry for pause/cancel/resume actions
# token → {vm_name, action, expires_at}
_confirmation_tokens: dict[str, dict] = {}
_confirmation_tokens_lock = threading.Lock()

_TOTAL_PHASES = len(ORDERED_PHASES)  # 13


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _phase_to_pct(phase_name: str, status: str) -> int:
    """Convert a phase name + status to a 0-100 progress percentage."""
    try:
        phase = Phase[phase_name]
    except KeyError:
        return 0
    if phase == Phase.COMPLETED:
        return 100
    if phase == Phase.FAILED:
        return 0
    try:
        idx = ORDERED_PHASES.index(phase)
    except ValueError:
        return 0
    base = int((idx / _TOTAL_PHASES) * 100)
    if status == "SUCCESS":
        return int(((idx + 1) / _TOTAL_PHASES) * 100)
    if status == "RUNNING":
        return base + 2
    return base


def _get_phase_description(phase_name: str, status: str) -> str:
    """Return a human-readable description of the current phase."""
    phase_descriptions = {
        "PREFLIGHT": "Pre-flight checks",
        "SNAPSHOT": "Creating VMware snapshot",
        "EXPORT": "Exporting VM from VMware",
        "CONVERT_DISK": "Converting disk format",
        "INJECT": "Injecting VirtIO drivers (Windows only)",
        "IMPORT": "Importing VM to Proxmox",
        "CLEANUP": "Cleaning up temporary files",
        "COMPLETED": "Migration complete",
        "FAILED": "Migration failed",
    }
    
    desc = phase_descriptions.get(phase_name, phase_name)
    
    if status == "RUNNING":
        return f"{desc}..."
    elif status == "SUCCESS":
        return f"{desc} ✓"
    elif status == "FAILED":
        return f"{desc} ✗"
    elif status == "PENDING":
        return f"Waiting for {desc.lower()}"
    
    return desc


def _build_migration_config(session: dict, mappings: list[ResourceMapping]):
    """Construct a MigrationConfig from session credentials and UI mappings.

    Builds synthetic network_map and storage_map entries from the cached VM
    inventory data (stored during the /api/vmware/vms call) and the resource
    mappings chosen in the UI.
    """
    from vmigrate.config import (
        MigrationConfig,
        MigrationSettings,
        NetworkMapping,
        ProxmoxConfig,
        StorageMapping,
        VMConfig,
        VMwareConfig,
    )

    vmw = session.get("vmware", {})
    prx = session.get("proxmox", {})

    vmware_cfg = VMwareConfig(
        host=vmw["host"],
        port=vmw.get("port", 443),
        username=vmw["username"],
        password=vmw["password"],
        datacenter=vmw["datacenter"],
        verify_ssl=vmw.get("verify_ssl", False),
    )
    proxmox_cfg = ProxmoxConfig(
        host=prx["host"],
        port=prx.get("port", 8006),
        user=prx["user"],
        password=prx["password"],
        node=prx["node"],
        verify_ssl=prx.get("verify_ssl", False),
        cluster_ips=prx.get("cluster_ips", []),
    )

    import tempfile, os as _os
    _tmp = Path(tempfile.gettempdir()) / "vmigrate"
    work_dir = Path(_os.environ.get("VMIGRATE_WORK_DIR", str(_tmp)))
    state_db_path = work_dir / "state.db"

    migration_settings = MigrationSettings(
        mode="cold",      # per-VM mode_override handles live
        work_dir=work_dir,
        state_db=state_db_path,
        max_parallel=2,
        retry_attempts=3,
        retry_delay_seconds=30,
    )

    # Build network_map and storage_map from cached VM data + user mappings
    # vm_cache: {vm_name: {disks: [...], nics: [...]}} stored by inventory route
    vm_cache: dict = session.get("vm_cache", {})

    portgroup_to_bridge: dict[str, str] = {}
    datastore_to_storage: dict[str, str] = {}

    vm_configs: list[VMConfig] = []
    missing_cache: list[str] = []

    for m in mappings:
        vm_configs.append(
            VMConfig(
                name=m.vm_name,
                target_node=m.target_node,
                mode_override=m.migration_mode if m.migration_mode != "cold" else None,
            )
        )
        cached = vm_cache.get(m.vm_name, {})

        # Map each NIC portgroup → selected bridge
        for nic in cached.get("nics", []):
            pg = nic.get("portgroup", "")
            if pg:
                portgroup_to_bridge[pg] = m.network_bridge

        # Map each disk datastore → selected storage
        for disk in cached.get("disks", []):
            ds = disk.get("datastore", "")
            if ds:
                datastore_to_storage[ds] = m.storage

        if not cached:
            missing_cache.append(m.vm_name)

    # If any VMs are missing from cache (session expired or VM list not loaded),
    # do a live lookup against vCenter to get the real datastore/portgroup names.
    if missing_cache:
        logger.info("vm_cache missing for %s — doing live VMware lookup", missing_cache)
        try:
            from vmigrate.vmware.client import VMwareClient
            from vmigrate.vmware.inventory import VMwareInventory

            live_cfg = VMwareConfig(
                host=vmw["host"], port=vmw.get("port", 443),
                username=vmw["username"], password=vmw["password"],
                datacenter=vmw["datacenter"], verify_ssl=vmw.get("verify_ssl", False),
            )
            live_client = VMwareClient(live_cfg)
            live_client.connect()
            inv = VMwareInventory(live_client)

            # Match missing VMs to their resource mapping
            mapping_by_name = {m.vm_name: m for m in mappings}
            for vm_name in missing_cache:
                m = mapping_by_name[vm_name]
                try:
                    vm_obj = inv.find_vm(vm_name, vmw["datacenter"])
                    vm_detail = inv.get_vm_info(vm_obj)
                    for nic in vm_detail.get("nics", []):
                        pg = nic.get("portgroup", "")
                        if pg:
                            portgroup_to_bridge[pg] = m.network_bridge
                    for disk in vm_detail.get("disks", []):
                        ds = disk.get("datastore", "")
                        if ds:
                            datastore_to_storage[ds] = m.storage
                except Exception as exc:
                    logger.warning("Live lookup failed for VM '%s': %s", vm_name, exc)

            live_client.disconnect()
        except Exception as exc:
            logger.warning("Live VMware lookup failed: %s — using placeholder mappings", exc)
            for m in mappings:
                if m.vm_name in missing_cache:
                    portgroup_to_bridge.setdefault(f"__pg_{m.vm_name}__", m.network_bridge)
                    datastore_to_storage.setdefault(f"__ds_{m.vm_name}__", m.storage)

    network_map = [
        NetworkMapping(vmware_portgroup=pg, proxmox_bridge=br)
        for pg, br in portgroup_to_bridge.items()
    ]
    storage_map = [
        StorageMapping(vmware_datastore=ds, proxmox_storage=st)
        for ds, st in datastore_to_storage.items()
    ]

    # Ensure at least one entry so config validates
    if not network_map:
        network_map = [NetworkMapping(vmware_portgroup="VM Network", proxmox_bridge="vmbr0")]
    if not storage_map:
        storage_map = [StorageMapping(vmware_datastore="datastore1", proxmox_storage="local-lvm")]

    return MigrationConfig(
        vmware=vmware_cfg,
        proxmox=proxmox_cfg,
        migration=migration_settings,
        network_map=network_map,
        storage_map=storage_map,
        vms=vm_configs,
    ), state_db_path


def _run_migration_worker(config, state_db: StateDB, vm_name: str) -> bool:
    """Worker function executed in a thread pool for a single VM."""
    try:
        from vmigrate.migration.orchestrator import MigrationOrchestrator
        orch = MigrationOrchestrator(config, state_db)
        results = orch.run(vm_names=[vm_name])
        return results.get(vm_name, False)
    except Exception as exc:
        logger.exception("Migration worker crashed for VM '%s': %s", vm_name, exc)
        try:
            state_db.transition(
                vm_name, Phase.FAILED, PhaseStatus.FAILED, error=str(exc)
            )
        except Exception:
            pass
        return False


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.post("/migrate/start", response_model=MigrationStartResponse)
async def start_migration(
    body: StartMigrationRequest,
    request: Request,
    response: Response,
) -> MigrationStartResponse:
    """Start background migration jobs for the requested VMs."""
    sid, session = get_or_create_session(request, response)

    if not session.get("vmware_connected"):
        raise HTTPException(status_code=401, detail="Not connected to VMware.")
    if not session.get("proxmox_connected"):
        raise HTTPException(status_code=401, detail="Not connected to Proxmox.")
    if not body.mappings:
        raise HTTPException(status_code=400, detail="No VM mappings provided.")

    try:
        config, state_db_path = _build_migration_config(session, body.mappings)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Failed to build migration config: {exc}")

    state_db_path.parent.mkdir(parents=True, exist_ok=True)
    state_db = StateDB(state_db_path)

    for m in body.mappings:
        existing = state_db.get_vm_state(m.vm_name)
        if existing:
            existing_status = existing.get("status", "")
            existing_phase  = existing.get("phase",  "")
            if existing_phase == "COMPLETED":
                # COMPLETED → only reset if user explicitly asked (Reset & Re-run button).
                # On a plain "Start Migration" click, leave COMPLETED VMs alone so they
                # are not unnecessarily re-run.
                logger.info(
                    "VM '%s' already COMPLETED — skipping (use Reset & Re-run to force).",
                    m.vm_name,
                )
            elif existing_status == "FAILED":
                # FAILED → resume from the failed phase, do NOT wipe artifacts.
                # Just clear the error and set status back to PENDING so the
                # orchestrator will re-try only the failed phase and everything after.
                logger.info(
                    "VM '%s' previously FAILED at phase %s — resuming from that phase.",
                    m.vm_name, existing_phase,
                )
                with state_db._conn:
                    state_db._conn.execute(
                        """UPDATE vm_state
                           SET status=?, error=NULL, updated_at=datetime('now')
                           WHERE vm_name=?""",
                        (PhaseStatus.PENDING.value, m.vm_name),
                    )
            # else: RUNNING/PENDING — leave as-is, the worker will handle it
        else:
            state_db.init_vm(m.vm_name)

    job_id = str(uuid.uuid4())
    futures: dict[str, Future] = {}

    for m in body.mappings:
        future = _executor.submit(_run_migration_worker, config, state_db, m.vm_name)
        futures[m.vm_name] = future

    with _jobs_lock:
        _jobs[job_id] = {
            "futures": futures,
            "state_db": state_db,
            "vm_names": [m.vm_name for m in body.mappings],
            "work_dir": str(config.migration.work_dir),
            "config": config,
        }

    session["active_job_id"] = job_id
    set_session(sid, session)

    logger.info("Started migration job %s for VMs: %s", job_id, [m.vm_name for m in body.mappings])
    return MigrationStartResponse(job_id=job_id, vm_names=[m.vm_name for m in body.mappings])


@router.get("/migrate/status", response_model=list[VMStatus])
async def migration_status(
    request: Request,
    response: Response,
    job_id: Optional[str] = None,
) -> list[VMStatus]:
    """Return migration status for all VMs in the active job."""
    sid, session = get_or_create_session(request, response)
    set_session(sid, session)

    active_job_id = job_id or session.get("active_job_id")
    if not active_job_id:
        return []

    with _jobs_lock:
        job = _jobs.get(active_job_id)

    if not job:
        return []

    state_db: StateDB = job["state_db"]
    result: list[VMStatus] = []

    for vm_name in job["vm_names"]:
        state = state_db.get_vm_state(vm_name)
        if not state:
            result.append(VMStatus(vm_name=vm_name, phase="PREFLIGHT", status="PENDING"))
            continue
        phase = state.get("phase", "PREFLIGHT")
        status_val = state.get("status", "PENDING")
        is_paused = state.get("paused", False)
        
        # Determine which actions are available
        can_pause = status_val == "RUNNING" and not is_paused
        can_resume = is_paused
        can_cancel = status_val in ("PENDING", "RUNNING")
        
        result.append(
            VMStatus(
                vm_name=vm_name,
                phase=phase,
                status=status_val,
                started_at=None,
                updated_at=state.get("updated_at"),
                error=state.get("error"),
                progress_pct=_phase_to_pct(phase, status_val),
                phase_description=_get_phase_description(phase, status_val),
                current_detail=state.get("current_detail", ""),
                paused=is_paused,
                can_pause=can_pause,
                can_cancel=can_cancel,
                can_resume=can_resume,
            )
        )

    return result


@router.post("/migrate/retry/{vm_name}")
async def retry_migration(
    vm_name: str,
    request: Request,
    response: Response,
) -> dict:
    """Resume a failed migration from its last successful checkpoint."""
    sid, session = get_or_create_session(request, response)
    set_session(sid, session)

    active_job_id = session.get("active_job_id")
    if not active_job_id:
        raise HTTPException(status_code=404, detail="No active migration job found.")

    with _jobs_lock:
        job = _jobs.get(active_job_id)

    if not job or vm_name not in job["vm_names"]:
        raise HTTPException(status_code=404, detail=f"VM '{vm_name}' not in active job.")

    state_db: StateDB = job["state_db"]
    state = state_db.get_vm_state(vm_name)
    if not state:
        raise HTTPException(status_code=404, detail=f"No state found for VM '{vm_name}'.")

    if PhaseStatus(state["status"]) != PhaseStatus.FAILED:
        raise HTTPException(
            status_code=400,
            detail=f"VM '{vm_name}' is not in FAILED state (current: {state['phase']}/{state['status']}).",
        )

    try:
        state_db.reset_to_checkpoint(vm_name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    # Resubmit to thread pool
    config = job["config"]
    future = _executor.submit(_run_migration_worker, config, state_db, vm_name)
    with _jobs_lock:
        job["futures"][vm_name] = future

    logger.info("Retrying migration for VM '%s' in job %s", vm_name, active_job_id)
    return {"success": True, "message": f"Retry started for VM '{vm_name}'."}


@router.post("/migrate/rollback/{vm_name}")
async def rollback_migration(
    vm_name: str,
    request: Request,
    response: Response,
) -> dict:
    """Roll back a failed migration, deleting any Proxmox VM and VMware snapshot."""
    sid, session = get_or_create_session(request, response)
    set_session(sid, session)

    active_job_id = session.get("active_job_id")
    if not active_job_id:
        raise HTTPException(status_code=404, detail="No active migration job found.")

    with _jobs_lock:
        job = _jobs.get(active_job_id)

    if not job or vm_name not in job["vm_names"]:
        raise HTTPException(status_code=404, detail=f"VM '{vm_name}' not in active job.")

    config = job["config"]
    state_db: StateDB = job["state_db"]

    try:
        from vmigrate.migration.orchestrator import MigrationOrchestrator
        orch = MigrationOrchestrator(config, state_db)
        orch.rollback(vm_name)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Rollback failed: {exc}")

    logger.info("Rolled back migration for VM '%s' in job %s", vm_name, active_job_id)
    return {"success": True, "message": f"Rollback complete for VM '{vm_name}'."}


@router.post("/migrate/stop")
async def stop_migration(
    request: Request,
    response: Response,
) -> dict:
    """Cancel pending (not yet started) VMs in the active job."""
    sid, session = get_or_create_session(request, response)
    set_session(sid, session)

    active_job_id = session.get("active_job_id")
    if not active_job_id:
        raise HTTPException(status_code=404, detail="No active migration job.")

    with _jobs_lock:
        job = _jobs.get(active_job_id)

    if not job:
        raise HTTPException(status_code=404, detail="Job not found.")

    cancelled = 0
    for vm_name, future in job["futures"].items():
        if future.cancel():
            cancelled += 1
            logger.info("Cancelled pending job for VM '%s'", vm_name)

    return {"success": True, "cancelled": cancelled,
            "message": f"Cancelled {cancelled} pending VM(s). Running VMs will complete."}


@router.get("/migrate/export-progress/{vm_name}")
async def export_progress(
    vm_name: str,
    request: Request,
    response: Response,
) -> dict:
    """Return current disk export progress for a VM (reads export_progress.json)."""
    sid, session = get_or_create_session(request, response)
    set_session(sid, session)

    import tempfile as _tf, os as _os
    _tmp = Path(_tf.gettempdir()) / "vmigrate"
    _work = Path(_os.environ.get("VMIGRATE_WORK_DIR", str(_tmp)))
    progress_file = _work / vm_name / "export_progress.json"
    if not progress_file.exists():
        return {}
    try:
        import json as _json
        return _json.loads(progress_file.read_text())
    except Exception:
        return {}


@router.post("/migrate/reset/{vm_name}")
async def reset_vm_state(
    vm_name: str,
    request: Request,
    response: Response,
) -> dict:
    """Reset a VM's migration state to PREFLIGHT so it will run all phases fresh.

    Use this when the state DB is stale (e.g. VM was manually deleted from
    Proxmox but the DB still shows COMPLETED).
    """
    sid, session = get_or_create_session(request, response)
    set_session(sid, session)

    active_job_id = session.get("active_job_id")
    if not active_job_id:
        raise HTTPException(status_code=404, detail="No active migration job found.")

    with _jobs_lock:
        job = _jobs.get(active_job_id)

    if not job or vm_name not in job["vm_names"]:
        raise HTTPException(status_code=404, detail=f"VM '{vm_name}' not in active job.")

    state_db: StateDB = job["state_db"]
    with state_db._conn:
        state_db._conn.execute(
            """UPDATE vm_state
               SET phase=?, status=?, error=NULL, artifacts='{}',
                   updated_at=datetime('now')
               WHERE vm_name=?""",
            (Phase.PREFLIGHT.name, PhaseStatus.PENDING.value, vm_name),
        )
        state_db._conn.execute(
            "INSERT INTO vm_history (vm_name, phase, status) VALUES (?, ?, 'RESET')",
            (vm_name, Phase.PREFLIGHT.name),
        )

    logger.info("Reset migration state for VM '%s' to PREFLIGHT/PENDING", vm_name)
    return {"success": True, "message": f"State reset for VM '{vm_name}'. Start a new migration to re-run all phases."}


@router.post("/migrate/confirm-action", response_model=ConfirmationResponse)
async def confirm_action(
    body: ConfirmationRequest,
    request: Request,
    response: Response,
) -> ConfirmationResponse:
    """Request confirmation for a pause/cancel/resume action.
    
    Returns a confirmation token that must be sent with the execute request.
    Token expires after 60 seconds.
    """
    sid, session = get_or_create_session(request, response)
    set_session(sid, session)

    # Validate action
    if body.action not in ("pause", "cancel", "resume"):
        raise HTTPException(status_code=400, detail=f"Invalid action: {body.action}")

    # Generate confirmation token
    token = str(uuid.uuid4())
    expires_at = time.time() + 60

    with _confirmation_tokens_lock:
        _confirmation_tokens[token] = {
            "vm_name": body.vm_name,
            "action": body.action,
            "expires_at": expires_at,
        }

    logger.info("Generated confirmation token for %s on VM '%s'", body.action, body.vm_name)
    
    return ConfirmationResponse(
        confirmation_token=token,
        action=body.action,
        message=f"Please confirm {body.action} for VM '{body.vm_name}'",
        expires_in_seconds=60,
    )


@router.post("/migrate/execute-action", response_model=ActionResponse)
async def execute_action(
    body: ExecuteActionRequest,
    request: Request,
    response: Response,
) -> ActionResponse:
    """Execute a pause/cancel/resume action with a valid confirmation token."""
    sid, session = get_or_create_session(request, response)
    set_session(sid, session)

    # Validate token
    with _confirmation_tokens_lock:
        token_data = _confirmation_tokens.pop(body.confirmation_token, None)

    if not token_data:
        raise HTTPException(status_code=401, detail="Invalid or expired confirmation token")

    # Check if token has expired
    if time.time() > token_data["expires_at"]:
        raise HTTPException(status_code=401, detail="Confirmation token has expired")

    # Verify token matches requested action and VM
    if token_data["vm_name"] != body.vm_name or token_data["action"] != body.action:
        raise HTTPException(status_code=400, detail="Token does not match requested action or VM")

    # Get job info
    active_job_id = session.get("active_job_id")
    if not active_job_id:
        raise HTTPException(status_code=404, detail="No active migration job found.")

    with _jobs_lock:
        job = _jobs.get(active_job_id)

    if not job or body.vm_name not in job["vm_names"]:
        raise HTTPException(status_code=404, detail=f"VM '{body.vm_name}' not in active job.")

    state_db: StateDB = job["state_db"]

    # Execute the action
    try:
        if body.action == "pause":
            with state_db._conn:
                state_db._conn.execute(
                    "UPDATE vm_state SET paused=1, updated_at=datetime('now') WHERE vm_name=?",
                    (body.vm_name,),
                )
            logger.info("Paused migration for VM '%s'", body.vm_name)
            return ActionResponse(
                success=True,
                vm_name=body.vm_name,
                action="pause",
                message=f"Migration paused for VM '{body.vm_name}'",
            )

        elif body.action == "resume":
            with state_db._conn:
                state_db._conn.execute(
                    "UPDATE vm_state SET paused=0, updated_at=datetime('now') WHERE vm_name=?",
                    (body.vm_name,),
                )
            logger.info("Resumed migration for VM '%s'", body.vm_name)
            return ActionResponse(
                success=True,
                vm_name=body.vm_name,
                action="resume",
                message=f"Migration resumed for VM '{body.vm_name}'",
            )

        elif body.action == "cancel":
            # Cancel by marking the future as cancelled (if not started)
            # or by setting state to CANCELLED if already running
            future = job["futures"].get(body.vm_name)
            cancelled_future = False
            if future and future.cancel():
                cancelled_future = True
                logger.info("Cancelled pending future for VM '%s'", body.vm_name)

            # Update state to track cancellation
            with state_db._conn:
                state_db._conn.execute(
                    """UPDATE vm_state 
                       SET status=?, updated_at=datetime('now')
                       WHERE vm_name=?""",
                    ("CANCELLED", body.vm_name),
                )

            logger.info("Cancelled migration for VM '%s' (future: %s)", body.vm_name, cancelled_future)
            return ActionResponse(
                success=True,
                vm_name=body.vm_name,
                action="cancel",
                message=f"Migration cancelled for VM '{body.vm_name}'",
            )

    except Exception as exc:
        logger.exception("Failed to execute %s for VM '%s': %s", body.action, body.vm_name, exc)
        raise HTTPException(status_code=500, detail=f"Failed to execute action: {exc}")
