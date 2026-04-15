"""Pydantic request/response models for the vmigrate Web UI API."""

from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Connection models
# ---------------------------------------------------------------------------


class VMwareConnectRequest(BaseModel):
    host: str
    port: int = 443
    username: str
    password: str
    datacenter: str
    verify_ssl: bool = False


class ProxmoxConnectRequest(BaseModel):
    host: str
    port: int = 8006
    user: str
    password: str
    node: str
    verify_ssl: bool = False
    cluster_ips: list[str] = Field(default_factory=list)  # IPs of all cluster nodes


class ConnectResponse(BaseModel):
    success: bool
    message: str
    details: dict[str, Any] = Field(default_factory=dict)


class ConnectionStatus(BaseModel):
    connected: bool
    host: Optional[str] = None


class ConnectionsStatusResponse(BaseModel):
    vmware: ConnectionStatus
    proxmox: ConnectionStatus


# ---------------------------------------------------------------------------
# Inventory models
# ---------------------------------------------------------------------------


class DiskInfo(BaseModel):
    label: str
    size_gb: float
    datastore: str


class NicInfo(BaseModel):
    label: str
    portgroup: str


class VMInfo(BaseModel):
    name: str
    guest_id: str
    guest_os: str          # human-readable string
    num_cpus: int
    memory_mb: int
    disks: list[DiskInfo]
    nics: list[NicInfo]
    power_state: str
    is_windows: bool


class ProxmoxNode(BaseModel):
    name: str
    status: str
    cpu_usage: float       # 0.0–1.0
    memory_used_gb: float
    memory_total_gb: float


class ProxmoxStorage(BaseModel):
    name: str
    type: str
    content: str
    avail_gb: float
    total_gb: float
    node: str


class ProxmoxNetwork(BaseModel):
    name: str    # bridge name, e.g. vmbr0
    node: str
    type: str
    active: bool


# ---------------------------------------------------------------------------
# Migration models
# ---------------------------------------------------------------------------


class ResourceMapping(BaseModel):
    vm_name: str
    target_node: str
    storage: str
    network_bridge: str
    migration_mode: str = "cold"   # "cold" | "live"


class StartMigrationRequest(BaseModel):
    mappings: list[ResourceMapping]


class VMStatus(BaseModel):
    vm_name: str
    phase: str
    status: str                     # PENDING / RUNNING / SUCCESS / FAILED
    started_at: Optional[str] = None
    updated_at: Optional[str] = None
    error: Optional[str] = None
    progress_pct: int = 0           # 0-100


class MigrationStartResponse(BaseModel):
    job_id: str
    vm_names: list[str]


# ---------------------------------------------------------------------------
# Helper: guest_id -> human-readable OS name
# ---------------------------------------------------------------------------

_GUEST_OS_MAP: dict[str, str] = {
    "windows9_64Guest": "Windows 10/11 (64-bit)",
    "windows9Guest": "Windows 10/11 (32-bit)",
    "windows8_64Guest": "Windows 8/8.1 (64-bit)",
    "windows8Guest": "Windows 8/8.1 (32-bit)",
    "windows7_64Guest": "Windows 7 (64-bit)",
    "windows7Guest": "Windows 7 (32-bit)",
    "winServer2022_64Guest": "Windows Server 2022",
    "winServer2019_64Guest": "Windows Server 2019",
    "winServer2016_64Guest": "Windows Server 2016",
    "windows8Server64Guest": "Windows Server 2012 R2",
    "windows7Server64Guest": "Windows Server 2008 R2",
    "rhel9_64Guest": "RHEL 9 (64-bit)",
    "rhel8_64Guest": "RHEL 8 (64-bit)",
    "rhel7_64Guest": "RHEL 7 (64-bit)",
    "rhel6_64Guest": "RHEL 6 (64-bit)",
    "centos8_64Guest": "CentOS 8 (64-bit)",
    "centos7_64Guest": "CentOS 7 (64-bit)",
    "centos6_64Guest": "CentOS 6 (64-bit)",
    "ubuntu64Guest": "Ubuntu (64-bit)",
    "ubuntuGuest": "Ubuntu (32-bit)",
    "debian11_64Guest": "Debian 11 (64-bit)",
    "debian10_64Guest": "Debian 10 (64-bit)",
    "debian9_64Guest": "Debian 9 (64-bit)",
    "debian8_64Guest": "Debian 8 (64-bit)",
    "oracleLinux9_64Guest": "Oracle Linux 9 (64-bit)",
    "oracleLinux8_64Guest": "Oracle Linux 8 (64-bit)",
    "oracleLinux7_64Guest": "Oracle Linux 7 (64-bit)",
    "sles15_64Guest": "SLES 15 (64-bit)",
    "sles12_64Guest": "SLES 12 (64-bit)",
    "other3xLinux64Guest": "Linux (64-bit)",
    "other4xLinux64Guest": "Linux (64-bit)",
    "otherLinux64Guest": "Linux (64-bit)",
    "otherLinuxGuest": "Linux (32-bit)",
    "other64Guest": "Other (64-bit)",
    "otherGuest": "Other",
    "otherGuest64": "Other (64-bit)",
}

_WINDOWS_PREFIXES = ("win", "Win")


def guest_id_to_os(guest_id: str) -> tuple[str, bool]:
    """Return (human_readable_os, is_windows) for a vSphere guestId."""
    human = _GUEST_OS_MAP.get(guest_id, guest_id)
    is_windows = any(guest_id.startswith(p) for p in _WINDOWS_PREFIXES)
    return human, is_windows
