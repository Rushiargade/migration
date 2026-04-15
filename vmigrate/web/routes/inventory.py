"""Inventory routes — list VMs, Proxmox nodes, storage, and networks.

GET /api/vmware/vms
GET /api/proxmox/nodes
GET /api/proxmox/storage?node=pve1
GET /api/proxmox/networks?node=pve1
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Request, Response

from vmigrate.web.app import get_or_create_session, set_session
from vmigrate.web.models import (
    DiskInfo,
    NicInfo,
    ProxmoxNetwork,
    ProxmoxNode,
    ProxmoxStorage,
    VMInfo,
    guest_id_to_os,
)

logger = logging.getLogger("vmigrate.web.inventory")

router = APIRouter(tags=["inventory"])

# Storage types that can hold VM disk images
_VM_STORAGE_TYPES = {"dir", "lvm", "lvmthin", "zfspool", "nfs", "cephfs", "rbd", "btrfs"}


def _require_session_connection(session: dict, system: str) -> dict:
    """Raise 401 if the system is not connected in the current session."""
    key = f"{system}_connected"
    if not session.get(key):
        raise HTTPException(
            status_code=401,
            detail=f"Not connected to {system}. Use POST /api/{system}/connect first.",
        )
    return session.get(system, {})


def _make_vmware_client(creds: dict):
    """Construct and connect a VMwareClient from session creds."""
    from vmigrate.config import VMwareConfig
    from vmigrate.vmware.client import VMwareClient

    cfg = VMwareConfig(
        host=creds["host"],
        port=creds["port"],
        username=creds["username"],
        password=creds["password"],
        datacenter=creds["datacenter"],
        verify_ssl=creds.get("verify_ssl", False),
    )
    client = VMwareClient(cfg)
    client.connect()
    return client, cfg


def _make_proxmox_client(creds: dict):
    """Construct and connect a ProxmoxClient from session creds."""
    from vmigrate.config import ProxmoxConfig
    from vmigrate.proxmox.client import ProxmoxClient

    cfg = ProxmoxConfig(
        host=creds["host"],
        port=creds["port"],
        user=creds["user"],
        password=creds["password"],
        node=creds["node"],
        verify_ssl=creds.get("verify_ssl", False),
    )
    client = ProxmoxClient(cfg)
    client.connect()
    return client, cfg


# ---------------------------------------------------------------------------
# VMware VMs
# ---------------------------------------------------------------------------


@router.get("/vmware/vms", response_model=list[VMInfo])
async def list_vmware_vms(
    request: Request,
    response: Response,
) -> list[VMInfo]:
    """List all VMs in the connected VMware datacenter."""
    sid, session = get_or_create_session(request, response)
    set_session(sid, session)

    creds = _require_session_connection(session, "vmware")

    try:
        client, cfg = _make_vmware_client(creds)
    except ImportError as exc:
        raise HTTPException(status_code=500, detail=f"pyVmomi not installed: {exc}")
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Cannot connect to VMware: {exc}")

    try:
        from vmigrate.vmware.inventory import VMwareInventory
        inv = VMwareInventory(client)

        try:
            raw_vms = inv.list_vms(cfg.datacenter)
        except Exception as exc:
            raise HTTPException(
                status_code=502,
                detail=f"Failed to list VMs from datacenter '{cfg.datacenter}': {exc}",
            )

        result: list[VMInfo] = []
        vm_cache: dict = {}   # vm_name → {disks: [...], nics: [...]}

        for vm_summary in raw_vms:
            guest_id = vm_summary.get("guest_id", "")
            guest_os, is_windows = guest_id_to_os(guest_id)

            # Try to get full disk/nic details; fall back to summary-only
            disks: list[DiskInfo] = []
            nics: list[NicInfo] = []
            raw_disks: list[dict] = []
            raw_nics: list[dict] = []
            try:
                vm_obj = inv.find_vm(vm_summary["name"], cfg.datacenter)
                vm_detail = inv.get_vm_info(vm_obj)
                raw_disks = vm_detail.get("disks", [])
                raw_nics  = vm_detail.get("nics",  [])
                disks = [
                    DiskInfo(
                        label=d["label"],
                        size_gb=d["size_gb"],
                        datastore=d["datastore"],
                    )
                    for d in raw_disks
                ]
                nics = [
                    NicInfo(label=n["label"], portgroup=n["portgroup"])
                    for n in raw_nics
                ]
            except Exception as exc:
                logger.debug("Could not get detailed info for VM '%s': %s", vm_summary["name"], exc)

            # Cache raw disk/NIC data keyed by VM name so migration.py
            # can build accurate storage_map and network_map entries.
            vm_cache[vm_summary["name"]] = {
                "disks": raw_disks,
                "nics":  raw_nics,
            }

            result.append(
                VMInfo(
                    name=vm_summary["name"],
                    guest_id=guest_id,
                    guest_os=guest_os,
                    num_cpus=vm_summary.get("num_cpus", 0),
                    memory_mb=vm_summary.get("memory_mb", 0),
                    disks=disks,
                    nics=nics,
                    power_state=vm_summary.get("power_state", "unknown"),
                    is_windows=is_windows,
                )
            )

        # Persist the cache so _build_migration_config can use real datastore
        # and portgroup names without reconnecting to vCenter.
        session["vm_cache"] = vm_cache
        set_session(sid, session)

        return result

    finally:
        try:
            client.disconnect()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Proxmox nodes
# ---------------------------------------------------------------------------


@router.get("/proxmox/nodes", response_model=list[ProxmoxNode])
async def list_proxmox_nodes(
    request: Request,
    response: Response,
) -> list[ProxmoxNode]:
    """List all nodes in the Proxmox cluster."""
    sid, session = get_or_create_session(request, response)
    set_session(sid, session)

    creds = _require_session_connection(session, "proxmox")

    try:
        client, _ = _make_proxmox_client(creds)
    except ImportError as exc:
        raise HTTPException(status_code=500, detail=f"proxmoxer not installed: {exc}")
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Cannot connect to Proxmox: {exc}")

    try:
        api = client.get_api()
        nodes_raw = api.nodes.get()  # type: ignore[union-attr]
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Failed to list Proxmox nodes: {exc}")

    result: list[ProxmoxNode] = []
    for node in nodes_raw:
        node_name = node.get("node", "")
        mem = node.get("mem", 0)
        maxmem = node.get("maxmem", 1) or 1
        mem_used_gb = round(mem / 1024**3, 2)
        mem_total_gb = round(maxmem / 1024**3, 2)
        cpu = node.get("cpu", 0.0)

        result.append(
            ProxmoxNode(
                name=node_name,
                status=node.get("status", "unknown"),
                cpu_usage=round(float(cpu), 4),
                memory_used_gb=mem_used_gb,
                memory_total_gb=mem_total_gb,
            )
        )
    return result


# ---------------------------------------------------------------------------
# Proxmox storage
# ---------------------------------------------------------------------------


@router.get("/proxmox/storage", response_model=list[ProxmoxStorage])
async def list_proxmox_storage(
    request: Request,
    response: Response,
    node: Optional[str] = Query(default=None, description="Node name; defaults to session node"),
) -> list[ProxmoxStorage]:
    """List storage pools on a Proxmox node that can hold VM disk images."""
    sid, session = get_or_create_session(request, response)
    set_session(sid, session)

    creds = _require_session_connection(session, "proxmox")
    target_node = node or creds.get("node", "")

    try:
        client, _ = _make_proxmox_client(creds)
    except ImportError as exc:
        raise HTTPException(status_code=500, detail=f"proxmoxer not installed: {exc}")
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Cannot connect to Proxmox: {exc}")

    try:
        api = client.get_api()
        storages_raw = api.nodes(target_node).storage.get()  # type: ignore[union-attr]
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Failed to list storage for node '{target_node}': {exc}",
        )

    result: list[ProxmoxStorage] = []
    for s in storages_raw:
        stype = s.get("type", "")
        # Only include storage types that can hold VM images
        if stype not in _VM_STORAGE_TYPES:
            continue
        content = s.get("content", "")
        # Only include if it stores images or disk images
        if "images" not in content and "rootdir" not in content:
            continue

        avail = s.get("avail", 0) or 0
        total = s.get("total", 0) or 0
        result.append(
            ProxmoxStorage(
                name=s.get("storage", ""),
                type=stype,
                content=content,
                avail_gb=round(avail / 1024**3, 2),
                total_gb=round(total / 1024**3, 2),
                node=target_node,
            )
        )
    return result


# ---------------------------------------------------------------------------
# Proxmox networks
# ---------------------------------------------------------------------------


@router.get("/proxmox/networks", response_model=list[ProxmoxNetwork])
async def list_proxmox_networks(
    request: Request,
    response: Response,
    node: Optional[str] = Query(default=None, description="Node name; defaults to session node"),
) -> list[ProxmoxNetwork]:
    """List Linux bridge network interfaces on a Proxmox node."""
    sid, session = get_or_create_session(request, response)
    set_session(sid, session)

    creds = _require_session_connection(session, "proxmox")
    target_node = node or creds.get("node", "")

    try:
        client, _ = _make_proxmox_client(creds)
    except ImportError as exc:
        raise HTTPException(status_code=500, detail=f"proxmoxer not installed: {exc}")
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Cannot connect to Proxmox: {exc}")

    try:
        api = client.get_api()
        ifaces_raw = api.nodes(target_node).network.get()  # type: ignore[union-attr]
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Failed to list network interfaces for node '{target_node}': {exc}",
        )

    result: list[ProxmoxNetwork] = []
    for iface in ifaces_raw:
        if iface.get("type") != "bridge":
            continue
        result.append(
            ProxmoxNetwork(
                name=iface.get("iface", ""),
                node=target_node,
                type=iface.get("type", "bridge"),
                active=bool(iface.get("active", 0)),
            )
        )
    return result
