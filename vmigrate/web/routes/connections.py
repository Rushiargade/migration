"""Connection test routes for VMware and Proxmox.

POST /api/vmware/connect  — test VMware connectivity, store session creds
POST /api/proxmox/connect — test Proxmox connectivity, store session creds
GET  /api/status/connections — return current connection state
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Request, Response

from vmigrate.web.app import get_or_create_session, set_session
from vmigrate.web.models import (
    ConnectResponse,
    ConnectionStatus,
    ConnectionsStatusResponse,
    ProxmoxConnectRequest,
    VMwareConnectRequest,
)

logger = logging.getLogger("vmigrate.web.connections")

router = APIRouter(tags=["connections"])


# ---------------------------------------------------------------------------
# VMware connect
# ---------------------------------------------------------------------------


@router.post("/vmware/connect", response_model=ConnectResponse)
async def vmware_connect(
    body: VMwareConnectRequest,
    request: Request,
    response: Response,
) -> ConnectResponse:
    """Test VMware vCenter connectivity and store credentials in the session."""
    sid, session = get_or_create_session(request, response)

    try:
        from vmigrate.config import VMwareConfig
        cfg = VMwareConfig(
            host=body.host,
            port=body.port,
            username=body.username,
            password=body.password,
            datacenter=body.datacenter,
            verify_ssl=body.verify_ssl,
        )

        try:
            from vmigrate.vmware.client import VMwareClient
        except ImportError as exc:
            return ConnectResponse(
                success=False,
                message=f"pyVmomi is not installed — cannot connect to VMware: {exc}",
            )

        client = VMwareClient(cfg)
        try:
            client.connect()
        except Exception as exc:
            err = str(exc)
            if "authentication" in err.lower() or "login" in err.lower() or "incorrect" in err.lower():
                msg = f"Authentication failed for user '{body.username}'. Check your credentials."
            elif "connect" in err.lower() or "timed out" in err.lower() or "refused" in err.lower():
                msg = f"Cannot reach host '{body.host}:{body.port}' — check firewall and host address."
            elif "ssl" in err.lower() or "certificate" in err.lower():
                msg = f"SSL/TLS error — try enabling 'Disable SSL verification': {err}"
            else:
                msg = f"Connection failed: {err}"
            return ConnectResponse(success=False, message=msg)

        # Fetch service content for version info
        details: dict = {}
        try:
            si = client.get_service_instance()
            content = si.content  # type: ignore[union-attr]
            about = content.about
            details = {
                "version": getattr(about, "version", "unknown"),
                "build": getattr(about, "build", "unknown"),
                "full_name": getattr(about, "fullName", "unknown"),
                "datacenter": body.datacenter,
            }
        except Exception as exc:
            logger.warning("Could not read vCenter version info: %s", exc)

        # Disconnect immediately — we reconnect per-request from stored creds
        try:
            client.disconnect()
        except Exception:
            pass

        # Persist credentials (not the live client — reconnect each time)
        session["vmware"] = {
            "host": body.host,
            "port": body.port,
            "username": body.username,
            "password": body.password,
            "datacenter": body.datacenter,
            "verify_ssl": body.verify_ssl,
        }
        session["vmware_connected"] = True
        set_session(sid, session)

        return ConnectResponse(
            success=True,
            message=f"Connected to vCenter {body.host}",
            details=details,
        )

    except Exception as exc:
        logger.exception("Unexpected error in vmware_connect")
        return ConnectResponse(success=False, message=f"Unexpected error: {exc}")


# ---------------------------------------------------------------------------
# Proxmox connect
# ---------------------------------------------------------------------------


@router.post("/proxmox/connect", response_model=ConnectResponse)
async def proxmox_connect(
    body: ProxmoxConnectRequest,
    request: Request,
    response: Response,
) -> ConnectResponse:
    """Test Proxmox VE connectivity and store credentials in the session."""
    sid, session = get_or_create_session(request, response)

    try:
        from vmigrate.config import ProxmoxConfig
        cfg = ProxmoxConfig(
            host=body.host,
            port=body.port,
            user=body.user,
            password=body.password,
            node=body.node,
            verify_ssl=body.verify_ssl,
        )

        try:
            from vmigrate.proxmox.client import ProxmoxClient
        except ImportError as exc:
            return ConnectResponse(
                success=False,
                message=f"proxmoxer is not installed — cannot connect to Proxmox: {exc}",
            )

        client = ProxmoxClient(cfg)
        try:
            client.connect()
        except Exception as exc:
            err = str(exc)
            if "401" in err or "auth" in err.lower() or "password" in err.lower():
                msg = f"Authentication failed for user '{body.user}'. Check your credentials."
            elif "connect" in err.lower() or "timed out" in err.lower() or "refused" in err.lower():
                msg = f"Cannot reach host '{body.host}:{body.port}' — check firewall and host address."
            elif "ssl" in err.lower() or "certificate" in err.lower():
                msg = f"SSL/TLS error — try enabling 'Disable SSL verification': {err}"
            else:
                msg = f"Connection failed: {err}"
            return ConnectResponse(success=False, message=msg)

        # Verify the target node and get its info
        details: dict = {}
        try:
            api = client.get_api()
            node_status = api.nodes(body.node).status.get()  # type: ignore[union-attr]
            mem_total = node_status.get("memory", {}).get("total", 0)
            mem_used = node_status.get("memory", {}).get("used", 0)
            mem_free_gb = round((mem_total - mem_used) / 1024**3, 2)
            details = {
                "node": body.node,
                "status": node_status.get("status", "unknown"),
                "pve_version": node_status.get("pveversion", "unknown"),
                "memory_free_gb": mem_free_gb,
            }
        except Exception as exc:
            logger.warning("Could not read Proxmox node info: %s", exc)
            details = {"node": body.node}

        # Persist credentials
        session["proxmox"] = {
            "host": body.host,
            "port": body.port,
            "user": body.user,
            "password": body.password,
            "node": body.node,
            "verify_ssl": body.verify_ssl,
            "cluster_ips": body.cluster_ips,
        }
        session["proxmox_connected"] = True
        set_session(sid, session)

        return ConnectResponse(
            success=True,
            message=f"Connected to Proxmox {body.host} (node: {body.node})",
            details=details,
        )

    except Exception as exc:
        logger.exception("Unexpected error in proxmox_connect")
        return ConnectResponse(success=False, message=f"Unexpected error: {exc}")


# ---------------------------------------------------------------------------
# Connection status
# ---------------------------------------------------------------------------


@router.get("/status/connections", response_model=ConnectionsStatusResponse)
async def connection_status(
    request: Request,
    response: Response,
) -> ConnectionsStatusResponse:
    """Return the current connection state for both source and destination."""
    sid, session = get_or_create_session(request, response)
    set_session(sid, session)  # refresh TTL

    vmware_creds = session.get("vmware", {})
    proxmox_creds = session.get("proxmox", {})

    return ConnectionsStatusResponse(
        vmware=ConnectionStatus(
            connected=session.get("vmware_connected", False),
            host=vmware_creds.get("host"),
        ),
        proxmox=ConnectionStatus(
            connected=session.get("proxmox_connected", False),
            host=proxmox_creds.get("host"),
        ),
    )
