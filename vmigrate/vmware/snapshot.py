"""VMware snapshot management and Change Block Tracking (CBT) support.

Provides snapshot lifecycle management and CBT-based changed-block queries
for efficient live/delta migration.
"""

from __future__ import annotations

import logging
from typing import Optional

from pyVmomi import vim  # type: ignore

from vmigrate.vmware.client import VMwareClient

logger = logging.getLogger("vmigrate.vmware.snapshot")


class SnapshotManager:
    """Create, remove, and query VMware snapshots for migration.

    Also manages Change Block Tracking (CBT) activation for live migrations
    that need to transfer only changed disk blocks.

    Example::

        mgr = SnapshotManager(client)
        moref = mgr.create_snapshot(vm, "vmigrate-baseline", quiesce=True)
        # ... export disks ...
        mgr.remove_snapshot(vm, moref)
    """

    def __init__(self, client: VMwareClient) -> None:
        """Initialise with a connected VMwareClient.

        Args:
            client: An active :class:`VMwareClient` instance.
        """
        self._client = client

    # ------------------------------------------------------------------
    # Snapshot lifecycle
    # ------------------------------------------------------------------

    def create_snapshot(
        self,
        vm: vim.VirtualMachine,
        name: str,
        quiesce: bool = True,
        memory: bool = False,
    ) -> str:
        """Create a snapshot on a VM and return its MoRef string.

        Args:
            vm: Target ``vim.VirtualMachine``.
            name: Human-readable snapshot name (e.g. "vmigrate-baseline").
            quiesce: Whether to quiesce the guest filesystem (requires VMware
                Tools).  Set ``False`` for VMs without Tools installed.
            memory: Whether to capture guest memory state.

        Returns:
            Snapshot MoRef string (``vim.ManagedObjectReference.value``).

        Raises:
            RuntimeError: If snapshot creation fails.
        """
        logger.info(
            "Creating snapshot '%s' on VM '%s' (quiesce=%s, memory=%s)",
            name,
            vm.name,
            quiesce,
            memory,
        )
        task = vm.CreateSnapshot_Task(
            name=name,
            description=f"vmigrate snapshot: {name}",
            memory=memory,
            quiesce=quiesce,
        )
        success = self._client.wait_for_task(task)

        # If quiesced snapshot failed, automatically retry without quiescing.
        # This happens when VMware Tools are not running or the guest OS does
        # not support VSS (common on Windows VMs without Tools installed).
        if not success and quiesce:
            logger.warning(
                "Quiesced snapshot failed for VM '%s'. "
                "Retrying without quiescing (crash-consistent snapshot). "
                "Ensure VMware Tools are installed for quiesced snapshots.",
                vm.name,
            )
            task = vm.CreateSnapshot_Task(
                name=name,
                description=f"vmigrate snapshot (no-quiesce): {name}",
                memory=memory,
                quiesce=False,
            )
            success = self._client.wait_for_task(task)

        if not success:
            raise RuntimeError(
                f"Failed to create snapshot '{name}' on VM '{vm.name}'. "
                "Check vCenter events for details."
            )

        # Retrieve the moref of the newly created snapshot
        moref = self._find_snapshot_moref(vm, name)
        if moref is None:
            raise RuntimeError(
                f"Snapshot '{name}' was created but its MoRef could not be "
                "located. This is unexpected - check vCenter for the snapshot."
            )
        logger.info(
            "Snapshot '%s' created for VM '%s' (moref=%s)", name, vm.name, moref
        )
        return moref

    def remove_snapshot(self, vm: vim.VirtualMachine, moref: str) -> None:
        """Remove a snapshot identified by its MoRef string.

        Args:
            vm: Target ``vim.VirtualMachine``.
            moref: Snapshot MoRef string as returned by :meth:`create_snapshot`.

        Raises:
            ValueError: If the snapshot cannot be found.
            RuntimeError: If removal fails.
        """
        snapshot_obj = self._get_snapshot_by_moref(vm, moref)
        if snapshot_obj is None:
            raise ValueError(
                f"Snapshot moref '{moref}' not found on VM '{vm.name}'. "
                "It may have already been removed."
            )
        logger.info("Removing snapshot moref=%s from VM '%s'", moref, vm.name)
        task = snapshot_obj.snapshot.RemoveSnapshot_Task(removeChildren=False)
        success = self._client.wait_for_task(task)
        if not success:
            raise RuntimeError(
                f"Failed to remove snapshot moref='{moref}' from VM '{vm.name}'."
            )
        logger.info("Snapshot removed from VM '%s'", vm.name)

    # ------------------------------------------------------------------
    # Change Block Tracking (CBT)
    # ------------------------------------------------------------------

    def enable_cbt(self, vm: vim.VirtualMachine) -> None:
        """Enable Change Block Tracking on a VM.

        CBT must be enabled before live migration delta syncs work.  Enabling
        CBT requires creating and immediately removing a temporary snapshot to
        take effect.

        Args:
            vm: Target ``vim.VirtualMachine``.

        Raises:
            RuntimeError: If the VM config cannot be updated.
        """
        logger.info("Enabling CBT on VM '%s'", vm.name)
        config_spec = vim.vm.ConfigSpec()
        config_spec.changeTrackingEnabled = True
        task = vm.ReconfigVM_Task(spec=config_spec)
        success = self._client.wait_for_task(task)
        if not success:
            raise RuntimeError(
                f"Failed to enable CBT on VM '{vm.name}'. "
                "Ensure the VM is not in a snapshot state and VMware Tools are "
                "installed."
            )

        # CBT takes effect only after a power cycle or snapshot create/remove
        logger.debug("Creating temp snapshot to activate CBT on '%s'", vm.name)
        temp_moref = self.create_snapshot(
            vm, "vmigrate-cbt-enable", quiesce=False, memory=False
        )
        self.remove_snapshot(vm, temp_moref)
        logger.info("CBT enabled on VM '%s'", vm.name)

    def get_change_id(
        self,
        vm: vim.VirtualMachine,
        snapshot_moref: str,
        disk_key: int,
    ) -> str:
        """Return the CBT change ID for a disk at a given snapshot.

        The change ID is used as a cursor when querying which blocks changed
        since that snapshot.

        Args:
            vm: Target ``vim.VirtualMachine``.
            snapshot_moref: Snapshot MoRef string.
            disk_key: Virtual disk device key.

        Returns:
            CBT change ID string (e.g. "52 a5d...").

        Raises:
            ValueError: If the disk or snapshot is not found.
        """
        snapshot_obj = self._get_snapshot_by_moref(vm, snapshot_moref)
        if snapshot_obj is None:
            raise ValueError(
                f"Snapshot moref '{snapshot_moref}' not found on VM '{vm.name}'."
            )
        snap_config = snapshot_obj.snapshot.config
        for dev in snap_config.hardware.device:
            if dev.key == disk_key:
                backing = dev.backing
                change_id = getattr(backing, "changeId", None)
                if change_id:
                    return change_id
                raise ValueError(
                    f"Disk key={disk_key} on VM '{vm.name}' does not have a CBT "
                    "changeId. Ensure CBT is enabled and a snapshot exists."
                )
        raise ValueError(
            f"Disk key={disk_key} not found in snapshot config of VM '{vm.name}'."
        )

    def query_changed_areas(
        self,
        vm: vim.VirtualMachine,
        disk_key: int,
        change_id: str,
        offset: int = 0,
    ) -> list[dict]:
        """Query disk extents that changed since the given CBT change ID.

        Args:
            vm: Target ``vim.VirtualMachine``.
            disk_key: Virtual disk device key.
            change_id: CBT change ID from a previous snapshot.
            offset: Byte offset to start querying from (for paginated queries).

        Returns:
            List of dicts with keys: start (int bytes), length (int bytes).
        """
        logger.debug(
            "Querying CBT changed areas: vm=%s disk_key=%d offset=%d",
            vm.name,
            disk_key,
            offset,
        )
        result = vm.QueryChangedDiskAreas(
            snapshot=None,
            deviceKey=disk_key,
            startOffset=offset,
            changeId=change_id,
        )
        areas = []
        for area in result.changedArea:
            areas.append({"start": area.start, "length": area.length})
        logger.debug(
            "CBT returned %d changed extents for disk_key=%d", len(areas), disk_key
        )
        return areas

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _find_snapshot_moref(
        self, vm: vim.VirtualMachine, name: str
    ) -> Optional[str]:
        """Find a snapshot by name and return its MoRef string."""
        if vm.snapshot is None:
            return None
        return self._search_snapshot_tree(vm.snapshot.rootSnapshotList, name)

    def _search_snapshot_tree(
        self, snapshots: list, name: str
    ) -> Optional[str]:
        """Recursively search snapshot tree for a matching name."""
        for snap_tree in snapshots:
            if snap_tree.name == name:
                return snap_tree.snapshot._moId  # type: ignore[union-attr]
            result = self._search_snapshot_tree(snap_tree.childSnapshotList, name)
            if result is not None:
                return result
        return None

    def _get_snapshot_by_moref(
        self, vm: vim.VirtualMachine, moref: str
    ) -> Optional[object]:
        """Find a snapshot tree entry by MoRef string."""
        if vm.snapshot is None:
            return None
        return self._search_snapshot_tree_by_moref(
            vm.snapshot.rootSnapshotList, moref
        )

    def _search_snapshot_tree_by_moref(
        self, snapshots: list, moref: str
    ) -> Optional[object]:
        """Recursively search snapshot tree for a MoRef match."""
        for snap_tree in snapshots:
            if snap_tree.snapshot._moId == moref:  # type: ignore[union-attr]
                return snap_tree
            result = self._search_snapshot_tree_by_moref(
                snap_tree.childSnapshotList, moref
            )
            if result is not None:
                return result
        return None
