"""Configuration loading and validation for vmigrate.

Loads YAML configuration, interpolates ${ENV_VAR} references from the
environment, and validates all required fields and mappings.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml


class ConfigError(Exception):
    """Raised when configuration is invalid or missing required fields.

    The message is always actionable - it tells the user exactly what to fix.
    """


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass
class VMwareConfig:
    """Connection parameters for VMware vSphere."""

    host: str
    port: int
    username: str
    password: str
    datacenter: str
    verify_ssl: bool = False


@dataclass
class ProxmoxConfig:
    """Connection parameters for Proxmox VE."""

    host: str
    port: int
    user: str
    password: str
    node: str
    verify_ssl: bool = False
    cluster_ips: list = field(default_factory=list)  # IPs of all cluster nodes for SSH


@dataclass
class NetworkMapping:
    """Maps a VMware portgroup to a Proxmox bridge."""

    vmware_portgroup: str
    proxmox_bridge: str
    vlan_tag: Optional[int] = None


@dataclass
class StorageMapping:
    """Maps a VMware datastore to a Proxmox storage pool."""

    vmware_datastore: str
    proxmox_storage: str
    format: str = "qcow2"


@dataclass
class VMConfig:
    """Per-VM migration configuration."""

    name: str
    target_node: str
    mode_override: Optional[str] = None
    post_migrate_script: Optional[str] = None


@dataclass
class MigrationSettings:
    """Global migration engine settings."""

    mode: str
    work_dir: Path
    state_db: Path
    max_parallel: int = 2
    retry_attempts: int = 3
    retry_delay_seconds: int = 30
    virtio_iso_path: Optional[str] = None
    conversion_host: Optional[str] = None
    conversion_host_user: str = "root"
    conversion_host_password: Optional[str] = None


@dataclass
class MigrationConfig:
    """Top-level configuration object holding all settings."""

    vmware: VMwareConfig
    proxmox: ProxmoxConfig
    migration: MigrationSettings
    network_map: list[NetworkMapping] = field(default_factory=list)
    storage_map: list[StorageMapping] = field(default_factory=list)
    vms: list[VMConfig] = field(default_factory=list)

    def get_network_mapping(self, portgroup: str) -> Optional[NetworkMapping]:
        """Return the NetworkMapping for a given VMware portgroup name, or None."""
        for nm in self.network_map:
            if nm.vmware_portgroup == portgroup:
                return nm
        return None

    def get_storage_mapping(self, datastore: str) -> Optional[StorageMapping]:
        """Return the StorageMapping for a given VMware datastore name, or None."""
        for sm in self.storage_map:
            if sm.vmware_datastore == datastore:
                return sm
        return None


# ---------------------------------------------------------------------------
# Environment variable interpolation
# ---------------------------------------------------------------------------

_ENV_VAR_RE = re.compile(r"\$\{([^}]+)\}")


def _interpolate(value: str) -> str:
    """Replace all ``${VAR}`` references with values from the environment.

    Raises ``ConfigError`` if a referenced variable is not set.
    """

    def _replace(match: re.Match) -> str:
        var_name = match.group(1)
        env_val = os.environ.get(var_name)
        if env_val is None:
            raise ConfigError(
                f"Environment variable '{var_name}' is referenced in the config "
                f"but is not set. Export it before running: export {var_name}=<value>"
            )
        return env_val

    return _ENV_VAR_RE.sub(_replace, value)


def _interpolate_dict(obj: object) -> object:
    """Recursively interpolate env vars in strings within dicts/lists."""
    if isinstance(obj, str):
        return _interpolate(obj)
    if isinstance(obj, dict):
        return {k: _interpolate_dict(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_interpolate_dict(item) for item in obj]
    return obj


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------


def _require(data: dict, key: str, section: str) -> object:
    """Return ``data[key]`` or raise ``ConfigError`` with a helpful message."""
    if key not in data or data[key] is None:
        raise ConfigError(
            f"Missing required key '{key}' in section '{section}' of the config file."
        )
    return data[key]


def _parse_vmware(raw: dict) -> VMwareConfig:
    return VMwareConfig(
        host=str(_require(raw, "host", "vmware")),
        port=int(raw.get("port", 443)),
        username=str(_require(raw, "username", "vmware")),
        password=str(_require(raw, "password", "vmware")),
        datacenter=str(_require(raw, "datacenter", "vmware")),
        verify_ssl=bool(raw.get("verify_ssl", False)),
    )


def _parse_proxmox(raw: dict) -> ProxmoxConfig:
    return ProxmoxConfig(
        host=str(_require(raw, "host", "proxmox")),
        port=int(raw.get("port", 8006)),
        user=str(_require(raw, "user", "proxmox")),
        password=str(_require(raw, "password", "proxmox")),
        node=str(_require(raw, "node", "proxmox")),
        verify_ssl=bool(raw.get("verify_ssl", False)),
    )


def _parse_migration(raw: dict) -> MigrationSettings:
    mode = str(raw.get("mode", "cold")).lower()
    if mode not in ("cold", "live"):
        raise ConfigError(
            f"migration.mode must be 'cold' or 'live', got '{mode}'."
        )
    work_dir = Path(str(_require(raw, "work_dir", "migration")))
    state_db = Path(str(_require(raw, "state_db", "migration")))
    return MigrationSettings(
        mode=mode,
        work_dir=work_dir,
        state_db=state_db,
        max_parallel=int(raw.get("max_parallel", 2)),
        retry_attempts=int(raw.get("retry_attempts", 3)),
        retry_delay_seconds=int(raw.get("retry_delay_seconds", 30)),
        virtio_iso_path=raw.get("virtio_iso_path"),
        conversion_host=raw.get("conversion_host"),
        conversion_host_user=str(raw.get("conversion_host_user", "root")),
        conversion_host_password=raw.get("conversion_host_password"),
    )


def _parse_network_map(raw_list: list) -> list[NetworkMapping]:
    mappings: list[NetworkMapping] = []
    for idx, entry in enumerate(raw_list):
        if not isinstance(entry, dict):
            raise ConfigError(
                f"network_map entry #{idx} must be a mapping, got {type(entry).__name__}."
            )
        portgroup = entry.get("vmware_portgroup")
        bridge = entry.get("proxmox_bridge")
        if not portgroup:
            raise ConfigError(
                f"network_map entry #{idx} is missing 'vmware_portgroup'. "
                "Every network_map entry must specify which VMware portgroup to map."
            )
        if not bridge:
            raise ConfigError(
                f"network_map entry #{idx} (portgroup '{portgroup}') is missing "
                "'proxmox_bridge'. Specify the Proxmox Linux bridge (e.g. vmbr0)."
            )
        vlan_tag = entry.get("vlan_tag")
        mappings.append(
            NetworkMapping(
                vmware_portgroup=str(portgroup),
                proxmox_bridge=str(bridge),
                vlan_tag=int(vlan_tag) if vlan_tag is not None else None,
            )
        )
    return mappings


def _parse_storage_map(raw_list: list) -> list[StorageMapping]:
    mappings: list[StorageMapping] = []
    for idx, entry in enumerate(raw_list):
        if not isinstance(entry, dict):
            raise ConfigError(
                f"storage_map entry #{idx} must be a mapping, got {type(entry).__name__}."
            )
        datastore = entry.get("vmware_datastore")
        storage = entry.get("proxmox_storage")
        if not datastore:
            raise ConfigError(
                f"storage_map entry #{idx} is missing 'vmware_datastore'."
            )
        if not storage:
            raise ConfigError(
                f"storage_map entry #{idx} (datastore '{datastore}') is missing "
                "'proxmox_storage'. Specify the Proxmox storage pool ID."
            )
        fmt = str(entry.get("format", "qcow2")).lower()
        if fmt not in ("qcow2", "raw"):
            raise ConfigError(
                f"storage_map entry #{idx}: format must be 'qcow2' or 'raw', got '{fmt}'."
            )
        mappings.append(
            StorageMapping(
                vmware_datastore=str(datastore),
                proxmox_storage=str(storage),
                format=fmt,
            )
        )
    return mappings


def _parse_vms(raw_list: list) -> list[VMConfig]:
    vms: list[VMConfig] = []
    for idx, entry in enumerate(raw_list):
        if not isinstance(entry, dict):
            raise ConfigError(
                f"vms entry #{idx} must be a mapping, got {type(entry).__name__}."
            )
        name = entry.get("name")
        target_node = entry.get("target_node")
        if not name:
            raise ConfigError(f"vms entry #{idx} is missing 'name'.")
        if not target_node:
            raise ConfigError(
                f"vms entry #{idx} (name '{name}') is missing 'target_node'. "
                "Specify the Proxmox node name where this VM should land."
            )
        mode_override = entry.get("mode_override")
        if mode_override is not None:
            mode_override = str(mode_override).lower()
            if mode_override not in ("cold", "live"):
                raise ConfigError(
                    f"vms['{name}'].mode_override must be 'cold' or 'live', "
                    f"got '{mode_override}'."
                )
        vms.append(
            VMConfig(
                name=str(name),
                target_node=str(target_node),
                mode_override=mode_override,
                post_migrate_script=entry.get("post_migrate_script"),
            )
        )
    return vms


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _validate(config: MigrationConfig) -> None:
    """Run cross-field validation on a fully parsed config.

    Raises ``ConfigError`` with an actionable message on any violation.
    """
    # Duplicate VM names
    names = [vm.name for vm in config.vms]
    seen: set[str] = set()
    for name in names:
        if name in seen:
            raise ConfigError(
                f"Duplicate VM name '{name}' in vms list. "
                "Each VM must appear only once."
            )
        seen.add(name)

    # Validate that live mode has a conversion host
    if config.migration.mode == "live" and not config.migration.conversion_host:
        raise ConfigError(
            "migration.mode is 'live' but migration.conversion_host is not set. "
            "Specify the hostname of the Linux conversion host."
        )

    # Check per-VM node names are consistent with the configured proxmox node
    # (warn-only: Proxmox clusters can have multiple nodes)

    if not config.vms:
        raise ConfigError(
            "No VMs defined in the 'vms' section. "
            "Add at least one VM entry with 'name' and 'target_node'."
        )

    if not config.network_map:
        raise ConfigError(
            "No entries in 'network_map'. "
            "Add at least one mapping from vmware_portgroup to proxmox_bridge."
        )

    if not config.storage_map:
        raise ConfigError(
            "No entries in 'storage_map'. "
            "Add at least one mapping from vmware_datastore to proxmox_storage."
        )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def load_config(path: Path) -> MigrationConfig:
    """Load, interpolate, parse, and validate a migration YAML config file.

    Args:
        path: Absolute or relative path to the YAML config file.

    Returns:
        A fully populated and validated ``MigrationConfig``.

    Raises:
        ConfigError: If the file is missing, malformed, or invalid.
        FileNotFoundError: If the config file does not exist.
    """
    if not path.exists():
        raise FileNotFoundError(
            f"Config file not found: {path}\n"
            "Copy config/migration.example.yaml to config/migration.yaml "
            "and fill in your environment values."
        )

    with path.open("r", encoding="utf-8") as fh:
        try:
            raw = yaml.safe_load(fh)
        except yaml.YAMLError as exc:
            raise ConfigError(
                f"Failed to parse YAML config at {path}: {exc}"
            ) from exc

    if not isinstance(raw, dict):
        raise ConfigError(
            f"Config file {path} must be a YAML mapping at the top level."
        )

    # Interpolate all ${ENV_VAR} placeholders
    raw = _interpolate_dict(raw)  # type: ignore[assignment]

    # Parse each top-level section
    vmware_raw = raw.get("vmware")
    if not vmware_raw:
        raise ConfigError("Missing 'vmware' section in config.")
    proxmox_raw = raw.get("proxmox")
    if not proxmox_raw:
        raise ConfigError("Missing 'proxmox' section in config.")
    migration_raw = raw.get("migration")
    if not migration_raw:
        raise ConfigError("Missing 'migration' section in config.")

    config = MigrationConfig(
        vmware=_parse_vmware(vmware_raw),
        proxmox=_parse_proxmox(proxmox_raw),
        migration=_parse_migration(migration_raw),
        network_map=_parse_network_map(raw.get("network_map") or []),
        storage_map=_parse_storage_map(raw.get("storage_map") or []),
        vms=_parse_vms(raw.get("vms") or []),
    )

    _validate(config)
    return config
