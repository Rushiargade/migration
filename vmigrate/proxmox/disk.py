"""Proxmox disk import and attachment for vmigrate.

Handles importing converted qcow2 images into Proxmox storage and attaching
them to VMs as virtual SCSI disks.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Optional

from vmigrate.proxmox.client import ProxmoxClient
from vmigrate.utils.ssh import SSHClient

logger = logging.getLogger("vmigrate.proxmox.disk")


class DiskManager:
    """Import and attach disk images on a Proxmox node.

    Disk import uses ``qm importdisk`` via SSH to the Proxmox node, which is
    the supported method for importing external disk images.  Disk attachment
    is done via the Proxmox REST API.

    Example::

        disk_mgr = DiskManager(client, ssh=proxmox_ssh)
        disk_id = disk_mgr.import_disk(200, "pve1", Path("/tmp/disk.qcow2"), "local-lvm")
        disk_mgr.attach_disk(200, "pve1", disk_id, controller="scsi", index=0)
        disk_mgr.set_boot_order(200, "pve1", "scsi0")
    """

    def __init__(
        self,
        client: ProxmoxClient,
        ssh: Optional[SSHClient] = None,
    ) -> None:
        """Initialise the disk manager.

        Args:
            client: An active :class:`ProxmoxClient` instance.
            ssh: Optional :class:`SSHClient` connected to the Proxmox node.
                Required for :meth:`import_disk` since ``qm importdisk``
                must be run on the Proxmox node itself.
        """
        self._client = client
        self._ssh = ssh

    # ------------------------------------------------------------------
    # Import
    # ------------------------------------------------------------------

    def import_disk(
        self,
        vmid: int,
        node: str,
        qcow2_path: Path,
        storage: str,
    ) -> str:
        """Import a qcow2 image into Proxmox storage.

        Runs ``qm importdisk {vmid} {path} {storage} --format qcow2`` on the
        Proxmox node via SSH.  After import, Proxmox adds the disk to the VM
        as an ``unused0``, ``unused1``, etc. volume.

        Args:
            vmid: Target Proxmox VMID.
            node: Proxmox node name.
            qcow2_path: Path to the qcow2 image (on the Proxmox node or
                accessible from it via NFS/CIFS).
            storage: Proxmox storage pool ID (e.g. "local-lvm").

        Returns:
            Disk identifier string, e.g. "unused0".

        Raises:
            RuntimeError: If the import fails or SSH is not configured.
        """
        if self._ssh is None:
            raise RuntimeError(
                "DiskManager requires an SSHClient to run 'qm importdisk'. "
                "Provide ssh= to DiskManager or configure a Proxmox SSH connection."
            )

        path_str = qcow2_path.as_posix() if hasattr(qcow2_path, 'as_posix') else str(qcow2_path).replace("\\", "/")

        # Detect storage type to pick the right disk format:
        #   lvm / lvmthin → raw  (block devices, no file format)
        #   dir / nfs / cephfs  → qcow2 (file-based, supports snapshots)
        #   rbd               → raw
        disk_format = "qcow2"
        try:
            api = self._client.get_api()
            storages = api.nodes(node).storage.get()  # type: ignore
            for s in storages:
                if s.get("storage") == storage:
                    stype = s.get("type", "")
                    if stype in ("lvm", "lvmthin", "rbd"):
                        disk_format = "raw"
                    break
        except Exception as exc:
            logger.warning("Could not detect storage type for '%s', defaulting to raw: %s", storage, exc)
            disk_format = "raw"  # safe default — works everywhere

        logger.info("Storage '%s' format: %s", storage, disk_format)
        cmd = f"qm importdisk {vmid} {path_str} {storage} --format {disk_format}"
        logger.info(
            "Importing disk into Proxmox: vmid=%d storage=%s path=%s",
            vmid,
            storage,
            qcow2_path.name,
        )

        rc, stdout, stderr = self._ssh.run(cmd, timeout=7200)

        if rc != 0:
            raise RuntimeError(
                f"'qm importdisk' failed (exit={rc}) for vmid={vmid}:\n"
                f"  command: {cmd}\n"
                f"  stderr: {stderr.strip()}\n"
                "Check that the qcow2 file exists on the Proxmox node and "
                "that the storage pool has sufficient free space."
            )

        # Parse the unused disk ID from the output
        # qm importdisk prints lines like: "unused0: local-lvm:vm-200-disk-0"
        disk_id = self._parse_unused_disk_id(stdout)
        logger.info("Disk imported as '%s' for vmid=%d", disk_id, vmid)
        return disk_id

    def _parse_unused_disk_id(self, output: str) -> str:
        """Extract the unused disk identifier from qm importdisk output.

        Args:
            output: stdout from ``qm importdisk``.

        Returns:
            Disk ID string like "unused0".
        """
        for line in output.splitlines():
            line = line.strip()
            if line.startswith("unused"):
                # e.g. "unused0: local-lvm:vm-200-disk-0"
                return line.split(":")[0].strip()
        # Fallback: assume unused0
        logger.warning(
            "Could not parse unused disk ID from qm importdisk output. "
            "Assuming 'unused0'. Output was:\n%s",
            output[:500],
        )
        return "unused0"

    # ------------------------------------------------------------------
    # Attachment
    # ------------------------------------------------------------------

    def attach_disk(
        self,
        vmid: int,
        node: str,
        disk_id: str,
        controller: str = "scsi",
        index: int = 0,
    ) -> None:
        """Attach an imported disk to a Proxmox VM.

        Moves the disk from ``unusedN`` to a controller slot (e.g. ``scsi0``).

        Args:
            vmid: Proxmox VMID.
            node: Proxmox node name.
            disk_id: Unused disk identifier from :meth:`import_disk`.
            controller: Controller type: "scsi", "virtio", "ide", "sata".
            index: Controller port index (0 = primary disk).

        Raises:
            RuntimeError: If the API call fails.
        """
        api = self._client.get_api()
        slot = f"{controller}{index}"

        # Read the current disk volume path from the VM config
        try:
            config = api.nodes(node).qemu(vmid).config.get()  # type: ignore[union-attr]
            disk_volume = config.get(disk_id)
        except Exception as exc:
            raise RuntimeError(
                f"Failed to read VM config for vmid={vmid}: {exc}"
            ) from exc

        if disk_volume is None:
            raise RuntimeError(
                f"Disk '{disk_id}' not found in VM vmid={vmid} config. "
                f"Available keys: {list(config.keys())}"
            )

        # Extract the volume name (e.g. "local-lvm:vm-200-disk-0")
        # disk_volume may be just the volume name
        volume = disk_volume.split(",")[0]

        logger.info(
            "Attaching disk %s -> %s for vmid=%d on node=%s",
            disk_id,
            slot,
            vmid,
            node,
        )

        try:
            api.nodes(node).qemu(vmid).config.put(  # type: ignore[union-attr]
                **{slot: f"{volume},iothread=1"}
            )
        except Exception as exc:
            raise RuntimeError(
                f"Failed to attach disk '{disk_id}' as '{slot}' for "
                f"vmid={vmid}: {exc}"
            ) from exc

        logger.info("Disk '%s' attached as '%s' for vmid=%d", disk_id, slot, vmid)

    # ------------------------------------------------------------------
    # Boot order
    # ------------------------------------------------------------------

    def set_boot_order(self, vmid: int, node: str, boot_disk: str) -> None:
        """Set the VM boot order to boot from a specific disk.

        Args:
            vmid: Proxmox VMID.
            node: Proxmox node name.
            boot_disk: Controller slot to boot from (e.g. "scsi0").

        Raises:
            RuntimeError: If the API call fails.
        """
        api = self._client.get_api()
        boot_order = f"order={boot_disk}"
        logger.info(
            "Setting boot order for vmid=%d: %s", vmid, boot_order
        )
        try:
            api.nodes(node).qemu(vmid).config.put(boot=boot_order)  # type: ignore[union-attr]
        except Exception as exc:
            raise RuntimeError(
                f"Failed to set boot order for vmid={vmid}: {exc}"
            ) from exc
