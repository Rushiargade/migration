"""Proxmox VM creation and deletion for vmigrate.

Creates empty Proxmox VMs with the correct hardware configuration derived
from VMware VM introspection data.
"""

from __future__ import annotations

import logging
from typing import Optional

from vmigrate.config import NetworkMapping, StorageMapping
from vmigrate.proxmox.client import ProxmoxClient

logger = logging.getLogger("vmigrate.proxmox.vm_create")

# VMware guest ID prefixes that indicate Windows guests
_WINDOWS_GUEST_PREFIXES = ("windows", "win")


class VMCreator:
    """Create and delete Proxmox VMs for migrated guests.

    Example::

        creator = VMCreator(client)
        vmid = creator.create_vm(
            vmid=200,
            node="pve1",
            vm_info={"name": "web-01", "num_cpus": 4, "memory_mb": 8192, ...},
            storage_map=[...],
            network_map=[...],
        )
    """

    def __init__(self, client: ProxmoxClient) -> None:
        """Initialise with a connected ProxmoxClient.

        Args:
            client: An active :class:`ProxmoxClient` instance.
        """
        self._client = client
        self._proxmox_client = client  # alias used by wait_for_task calls

    # ------------------------------------------------------------------
    # Firmware mapping
    # ------------------------------------------------------------------

    def _map_firmware(self, vm_info: dict) -> dict:
        """Map VMware firmware settings to Proxmox BIOS/OVMF parameters.

        Args:
            vm_info: VM info dict from :meth:`VMwareInventory.get_vm_info`.

        Returns:
            Dict with keys ``bios`` and ``machine`` for the Proxmox API.
        """
        firmware = vm_info.get("firmware", "bios")
        if firmware == "efi":
            return {"bios": "ovmf", "machine": "q35"}
        return {"bios": "seabios", "machine": "i440fx"}

    def _is_windows(self, guest_id: str) -> bool:
        """Return True if the guest ID indicates a Windows VM."""
        guest_id_lower = guest_id.lower()
        return any(guest_id_lower.startswith(p) for p in _WINDOWS_GUEST_PREFIXES)

    def _pick_efi_storage(self, node: str, storage_map: list) -> str:
        """Pick the best storage for the EFI disk on this node.

        Queries the node's available storages and returns the ID of the first
        one that supports disk images and has free space. Falls back to the
        first storage in storage_map if nothing suitable is found.

        Preference order: local-lvm → lvmthin → lvm → dir → anything
        """
        api = self._client.get_api()
        try:
            storages = api.nodes(node).storage.get(content="images")  # type: ignore
            # Sort by preference: lvm types first (fast, no fragmentation)
            _pref = {"lvm": 0, "lvmthin": 1, "dir": 2, "zfspool": 3}
            storages_sorted = sorted(
                [s for s in storages if s.get("active", 0) and s.get("avail", 1) > 0],
                key=lambda s: (_pref.get(s.get("type", "z"), 9), s["storage"] != "local-lvm"),
            )
            if storages_sorted:
                chosen = storages_sorted[0]["storage"]
                logger.debug(
                    "EFI storage candidates on node '%s': %s — chose '%s'",
                    node, [s["storage"] for s in storages_sorted], chosen,
                )
                return chosen
        except Exception as exc:
            logger.warning("Could not query storages on node '%s': %s", node, exc)

        # Fallback: use first entry from storage_map
        if storage_map:
            return storage_map[0].proxmox_storage
        return "local-lvm"

    # ------------------------------------------------------------------
    # VM creation
    # ------------------------------------------------------------------

    def create_vm(
        self,
        vmid: int,
        node: str,
        vm_info: dict,
        storage_map: list[StorageMapping],
        network_map: list[NetworkMapping],
    ) -> int:
        """Create an empty Proxmox VM shell for the migrated guest.

        Creates the VM with the correct CPU, memory, firmware, and machine
        type based on the VMware VM's configuration.  Disks and NICs are
        added in separate steps by :class:`DiskManager` and
        :class:`NetworkManager`.

        Args:
            vmid: Proxmox VMID to assign.
            node: Proxmox node name.
            vm_info: VM hardware info dict from VMwareInventory.
            storage_map: List of storage mappings (for EFI disk placement).
            network_map: List of network mappings (informational).

        Returns:
            The assigned ``vmid``.

        Raises:
            RuntimeError: If VM creation fails.
        """
        api = self._client.get_api()
        firmware_params = self._map_firmware(vm_info)
        guest_id = vm_info.get("guest_id", "")
        is_windows = self._is_windows(guest_id)

        # Build the VM creation payload
        params: dict = {
            "vmid": vmid,
            "name": vm_info["name"],
            "cores": vm_info.get("num_cpus", 1),
            "memory": vm_info.get("memory_mb", 1024),
            "bios": firmware_params["bios"],
            "machine": firmware_params["machine"],
            "cpu": "host",
            "scsihw": "virtio-scsi-pci",
            "boot": "order=scsi0",
            "agent": 0,  # will be enabled later
            "onboot": 0,
        }

        # OS type hint for Proxmox
        if is_windows:
            params["ostype"] = "win10"  # sensible default for modern Windows
        else:
            params["ostype"] = "l26"  # Linux 2.6+

        # For OVMF/UEFI we need a small EFI disk (4 MB).
        # Pick the best available storage on this node automatically.
        if firmware_params["bios"] == "ovmf":
            efi_storage = self._pick_efi_storage(node, storage_map)
            logger.info("Using storage '%s' for EFI disk on node '%s'", efi_storage, node)
            params["efidisk0"] = f"{efi_storage}:4,efitype=4m,pre-enrolled-keys=1"

        logger.info(
            "Creating Proxmox VM: vmid=%d name='%s' node=%s bios=%s machine=%s",
            vmid,
            vm_info["name"],
            node,
            firmware_params["bios"],
            firmware_params["machine"],
        )

        try:
            result = api.nodes(node).qemu.post(**params)  # type: ignore[union-attr]
            # Proxmox VM creation is async — result is a task UPID string.
            # Wait for it to complete before returning so callers can
            # immediately run follow-up operations like qm importdisk.
            if isinstance(result, str) and result.startswith("UPID:"):
                logger.info("Waiting for VM creation task to complete: %s", result)
                self._client.wait_for_task(node, result, timeout=120)
            else:
                # Some proxmoxer versions block automatically — add a small
                # safety sleep to let the conf file be written to disk.
                import time as _time
                _time.sleep(3)
        except RuntimeError:
            raise
        except Exception as exc:
            raise RuntimeError(
                f"Failed to create Proxmox VM vmid={vmid} ('{vm_info['name']}') "
                f"on node '{node}': {exc}\n"
                "Check that the VMID is not already in use and that the node "
                "has sufficient resources."
            ) from exc

        logger.info(
            "Proxmox VM created: vmid=%d name='%s'", vmid, vm_info["name"]
        )
        return vmid

    # ------------------------------------------------------------------
    # VM deletion (rollback)
    # ------------------------------------------------------------------

    def delete_vm(self, vmid: int, node: str) -> None:
        """Delete a Proxmox VM, including all its disks.

        Used for rollback when migration fails after VM creation.

        Args:
            vmid: Proxmox VMID to delete.
            node: Proxmox node where the VM lives.

        Raises:
            RuntimeError: If deletion fails.
        """
        api = self._client.get_api()
        logger.warning(
            "Rolling back: deleting Proxmox VM vmid=%d on node=%s", vmid, node
        )
        try:
            # Stop the VM first if it's running
            try:
                status = api.nodes(node).qemu(vmid).status.current.get()  # type: ignore[union-attr]
                if status.get("status") == "running":
                    api.nodes(node).qemu(vmid).status.stop.post()  # type: ignore[union-attr]
                    logger.info("Stopped VM vmid=%d before deletion", vmid)
            except Exception:
                pass  # VM may not exist or may already be stopped

            api.nodes(node).qemu(vmid).delete()  # type: ignore[union-attr]
            logger.info("Deleted Proxmox VM vmid=%d", vmid)
        except Exception as exc:
            raise RuntimeError(
                f"Failed to delete Proxmox VM vmid={vmid} on node '{node}': {exc}"
            ) from exc
