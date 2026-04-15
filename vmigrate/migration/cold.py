"""Cold migration engine for vmigrate.

Cold migration powers off the VMware VM (or uses a snapshot), exports all
disks, converts them, imports into Proxmox, and starts the new VM.  Because
the source VM is offline during the bulk of the transfer there is no need for
delta synchronisation.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import Callable, Optional

from vmigrate.config import MigrationConfig, VMConfig
from vmigrate.conversion.qemu_img import QemuImgConverter
from vmigrate.conversion.virt_v2v import VirtV2VConverter
from vmigrate.logging_setup import phase_log, setup_logging
from vmigrate.proxmox.agent import AgentInstaller
from vmigrate.proxmox.client import ProxmoxClient
from vmigrate.proxmox.disk import DiskManager
from vmigrate.proxmox.network import NetworkManager
from vmigrate.proxmox.vm_create import VMCreator
from vmigrate.state import Phase, PhaseStatus, StateDB
from vmigrate.utils.checksum import verify_qcow2
from vmigrate.utils.retry import retry
from vmigrate.utils.ssh import SSHClient
from vmigrate.vmware.client import VMwareClient
from vmigrate.vmware.export import VMwareExporter
from vmigrate.vmware.inventory import VMwareInventory
from vmigrate.vmware.snapshot import SnapshotManager

logger = logging.getLogger("vmigrate.migration.cold")

_WINDOWS_PREFIXES = ("windows", "win")


def _is_windows(guest_id: str) -> bool:
    return any(guest_id.lower().startswith(p) for p in _WINDOWS_PREFIXES)


class ColdMigration:
    """Execute a cold (offline snapshot) migration of a VMware VM to Proxmox.

    Each phase is implemented as a private method and wrapped with state
    persistence so that a failed migration can be resumed from the last
    successful phase.

    Example::

        migration = ColdMigration(config, vm_config, state_db)
        success = migration.run()
    """

    def __init__(
        self,
        config: MigrationConfig,
        vm_config: VMConfig,
        state: StateDB,
    ) -> None:
        """Initialise the cold migration.

        Args:
            config: Global :class:`MigrationConfig`.
            vm_config: Per-VM :class:`VMConfig`.
            state: Shared :class:`StateDB` instance.
        """
        self.config = config
        self.vm_config = vm_config
        self.state = state
        self.vm_name = vm_config.name

        # Per-VM logger
        self.logger = setup_logging(
            self.vm_name,
            config.migration.work_dir,
        )

        # Working directory for this VM's artifacts
        self.vm_work_dir: Path = config.migration.work_dir / self.vm_name
        self.vm_work_dir.mkdir(parents=True, exist_ok=True)

        # These are populated during the run
        self._vmware_client: Optional[VMwareClient] = None
        self._proxmox_client: Optional[ProxmoxClient] = None
        self._proxmox_ssh: Optional[SSHClient] = None
        self._conversion_ssh: Optional[SSHClient] = None
        self._vm_info: Optional[dict] = None

    # ------------------------------------------------------------------
    # Phase runner
    # ------------------------------------------------------------------

    def _run_phase(self, phase: Phase, fn: Callable) -> bool:
        """Execute a phase function, handling state transitions.

        Marks the phase as RUNNING before calling ``fn()``.  On success,
        transitions to SUCCESS.  On failure, transitions to FAILED and returns
        False.

        Args:
            phase: The :class:`Phase` to execute.
            fn: Callable implementing the phase logic.

        Returns:
            ``True`` if the phase succeeded, ``False`` otherwise.
        """
        # Skip already-successful phases (resume support)
        current = self.state.get_vm_state(self.vm_name)
        if current:
            current_phase = Phase[current["phase"]]
            current_status = PhaseStatus(current["status"])
            if (
                current_phase.value > phase.value
                or (current_phase == phase and current_status == PhaseStatus.SUCCESS)
            ):
                self.logger.info(
                    "Skipping phase %s (already SUCCESS)", phase.name
                )
                return True

        phase_log(self.logger, phase.name, self.vm_name, "RUNNING")
        self.state.transition(self.vm_name, phase, PhaseStatus.RUNNING)

        try:
            fn()
            self.state.transition(self.vm_name, phase, PhaseStatus.SUCCESS)
            phase_log(self.logger, phase.name, self.vm_name, "SUCCESS")
            return True
        except Exception as exc:
            error_msg = str(exc)
            self.state.transition(
                self.vm_name, phase, PhaseStatus.FAILED, error=error_msg
            )
            phase_log(
                self.logger,
                phase.name,
                self.vm_name,
                "FAILED",
                error=error_msg[:200],
            )
            self.logger.exception("Phase %s failed for VM '%s'", phase.name, self.vm_name)
            return False

    # ------------------------------------------------------------------
    # Main entry point
    # ------------------------------------------------------------------

    def run(self) -> bool:
        """Execute all migration phases in order.

        Phases already marked SUCCESS in the state DB are skipped (resume).

        Returns:
            ``True`` if migration completed successfully, ``False`` otherwise.
        """
        self.logger.info(
            "Starting cold migration for VM '%s'", self.vm_name
        )
        self.state.init_vm(self.vm_name)

        phases: list[tuple[Phase, Callable]] = [
            (Phase.PREFLIGHT, self._preflight),
            (Phase.SNAPSHOT_CREATE, self._snapshot_create),
            (Phase.EXPORT_DISK, self._export_disk),
            (Phase.CONVERT_DISK, self._convert_disk),
            (Phase.VERIFY_DISK, self._verify_disk),
            (Phase.PROXMOX_VM_CREATE, self._proxmox_vm_create),
            (Phase.PROXMOX_DISK_IMPORT, self._proxmox_disk_import),
            (Phase.DRIVER_INJECT, self._driver_inject),
            (Phase.PROXMOX_NETWORK, self._proxmox_network),
            (Phase.PROXMOX_START, self._proxmox_start),
            (Phase.SNAPSHOT_REMOVE, self._snapshot_remove),
            (Phase.AGENT_INSTALL, self._agent_install),
        ]

        try:
            self._connect_clients()
            for phase, fn in phases:
                success = self._run_phase(phase, fn)
                if not success:
                    self.state.transition(
                        self.vm_name, Phase.FAILED, PhaseStatus.FAILED
                    )
                    return False

            self.state.transition(
                self.vm_name, Phase.COMPLETED, PhaseStatus.SUCCESS
            )
            self.logger.info(
                "Cold migration COMPLETED for VM '%s'", self.vm_name
            )
            return True
        except Exception as exc:
            self.logger.exception(
                "Unexpected error in migration for VM '%s': %s",
                self.vm_name,
                exc,
            )
            return False
        finally:
            self._disconnect_clients()

    # ------------------------------------------------------------------
    # Client management
    # ------------------------------------------------------------------

    def _connect_clients(self) -> None:
        """Open connections to VMware and Proxmox."""
        self._vmware_client = VMwareClient(self.config.vmware)
        self._vmware_client.connect()

        self._proxmox_client = ProxmoxClient(self.config.proxmox)
        self._proxmox_client.connect()

        # SSH to Proxmox node for disk import (use API password as SSH password)
        prx_user = self.config.proxmox.user.split("@")[0]  # "root@pam" → "root"
        self._proxmox_ssh = SSHClient(
            host=self.config.proxmox.host,
            user=prx_user,
            password=self.config.proxmox.password,
        )
        self._proxmox_ssh.connect()

        # SSH to conversion host if configured
        conv_host = self.config.migration.conversion_host
        if conv_host:
            self._conversion_ssh = SSHClient(
                host=conv_host,
                user=self.config.migration.conversion_host_user,
                password=self.config.proxmox.password,
            )
            self._conversion_ssh.connect()

    def _disconnect_clients(self) -> None:
        """Close all open connections."""
        for client in (
            self._vmware_client,
            self._proxmox_ssh,
            self._conversion_ssh,
        ):
            if client is not None:
                try:
                    client.disconnect() if hasattr(client, "disconnect") else client.close()
                except Exception:
                    pass
        self._vmware_client = None
        self._proxmox_client = None
        self._proxmox_ssh = None
        self._conversion_ssh = None

    # ------------------------------------------------------------------
    # Phase implementations
    # ------------------------------------------------------------------

    def _preflight(self) -> None:
        """Verify all prerequisites before starting migration."""
        assert self._vmware_client is not None
        assert self._proxmox_client is not None

        # Find the VM in vCenter
        inventory = VMwareInventory(self._vmware_client)
        vm = inventory.find_vm(self.vm_name, self.config.vmware.datacenter)
        self._vm_info = inventory.get_vm_info(vm)
        self.logger.info(
            "Preflight OK: VM '%s' found (cpus=%d mem=%dMB disks=%d nics=%d)",
            self.vm_name,
            self._vm_info["num_cpus"],
            self._vm_info["memory_mb"],
            len(self._vm_info["disks"]),
            len(self._vm_info["nics"]),
        )

        # Verify Proxmox node
        target_node = self.vm_config.target_node
        if not self._proxmox_client.verify_node(target_node):
            raise RuntimeError(
                f"Proxmox node '{target_node}' is not online. "
                "Check the Proxmox cluster status."
            )

        # Verify all disk datastores have a storage mapping
        for disk in self._vm_info["disks"]:
            ds = disk["datastore"]
            mapping = self.config.get_storage_mapping(ds)
            if mapping is None:
                raise RuntimeError(
                    f"No storage_map entry for datastore '{ds}' used by disk "
                    f"'{disk['label']}' of VM '{self.vm_name}'. "
                    "Add a storage_map entry in your config file."
                )

        # Verify all NIC portgroups have a network mapping
        for nic in self._vm_info["nics"]:
            pg = nic["portgroup"]
            mapping = self.config.get_network_mapping(pg)
            if mapping is None:
                raise RuntimeError(
                    f"No network_map entry for portgroup '{pg}' used by NIC "
                    f"'{nic['label']}' of VM '{self.vm_name}'. "
                    "Add a network_map entry in your config file."
                )

        # Store vm_info for use by later phases
        self.state.set_artifact(self.vm_name, "vm_info", self._vm_info)

    def _snapshot_create(self) -> None:
        """Create a VMware snapshot for consistent disk export.

        Strategy:
        1. If VM is powered off  → skip snapshot (disks are already consistent).
        2. If VM is powered on   → try quiesced snapshot first, fall back to
           crash-consistent (quiesce=False), and as a last resort power off the
           VM to export without any snapshot.
        """
        assert self._vmware_client is not None

        inventory = VMwareInventory(self._vmware_client)
        vm = inventory.find_vm(self.vm_name, self.config.vmware.datacenter)

        power_state = str(getattr(vm, "runtime", None) and vm.runtime.powerState or "")
        is_powered_off = "poweredOff" in power_state

        if is_powered_off:
            self.logger.info(
                "VM '%s' is powered off — skipping snapshot (disks are consistent).",
                self.vm_name,
            )
            self.state.set_artifact(self.vm_name, "snapshot_moref", None)
            self.state.set_artifact(self.vm_name, "snapshot_name", None)
            self.state.set_artifact(self.vm_name, "snapshot_skipped", True)
            return

        snap_mgr = SnapshotManager(self._vmware_client)
        snap_name = f"vmigrate-{self.vm_name}"

        # Try quiesced first, fall back to crash-consistent
        for quiesce in (True, False):
            try:
                moref = snap_mgr.create_snapshot(
                    vm, snap_name, quiesce=quiesce, memory=False
                )
                self.state.set_artifact(self.vm_name, "snapshot_moref", moref)
                self.state.set_artifact(self.vm_name, "snapshot_name", snap_name)
                self.state.set_artifact(self.vm_name, "snapshot_skipped", False)
                self.logger.info(
                    "Snapshot '%s' created (moref=%s, quiesce=%s)",
                    snap_name, moref, quiesce,
                )
                return
            except Exception as exc:
                if quiesce:
                    self.logger.warning(
                        "Quiesced snapshot failed for VM '%s': %s — "
                        "retrying without quiescing (crash-consistent).",
                        self.vm_name, exc,
                    )
                else:
                    self.logger.warning(
                        "Crash-consistent snapshot also failed for VM '%s': %s — "
                        "VM may have independent disks. Attempting graceful power-off "
                        "to export without snapshot.",
                        self.vm_name, exc,
                    )

        # Both snapshot attempts failed. Power off VM to get a consistent export.
        self.logger.warning(
            "All snapshot attempts failed for VM '%s'. "
            "Powering off VM to allow direct disk export.",
            self.vm_name,
        )
        try:
            task = vm.PowerOffVM_Task()
            self._vmware_client.wait_for_task(task)
            self.logger.info("VM '%s' powered off for direct disk export.", self.vm_name)
            self.state.set_artifact(self.vm_name, "snapshot_moref", None)
            self.state.set_artifact(self.vm_name, "snapshot_name", None)
            self.state.set_artifact(self.vm_name, "snapshot_skipped", True)
            self.state.set_artifact(self.vm_name, "powered_off_by_vmigrate", True)
        except Exception as exc:
            raise RuntimeError(
                f"Cannot snapshot or power off VM '{self.vm_name}': {exc}. "
                "The VM may have independent-mode disks. "
                "Power it off manually in vCenter, then retry."
            ) from exc

    @retry(attempts=3, delay=30, exceptions=(Exception,))
    def _export_disk(self) -> None:
        """Export all VM disks from VMware to the local work directory."""
        assert self._vmware_client is not None

        inventory = VMwareInventory(self._vmware_client)
        vm = inventory.find_vm(self.vm_name, self.config.vmware.datacenter)
        moref = self.state.get_artifact(self.vm_name, "snapshot_moref")

        exporter = VMwareExporter(self._vmware_client)
        exported = exporter.export_vm_disks(
            vm,
            output_dir=self.vm_work_dir,
            snapshot_moref=str(moref) if moref else None,
        )

        # Store exported disk paths as artifacts
        disk_artifacts = [
            {
                "label": d["label"],
                "local_path": str(d["local_path"]),
                "size_bytes": d["size_bytes"],
                "disk_key": d.get("disk_key"),
            }
            for d in exported
        ]
        self.state.set_artifact(self.vm_name, "exported_disks", disk_artifacts)
        self.logger.info("Exported %d disk(s) for VM '%s'", len(exported), self.vm_name)

    def _convert_disk(self) -> None:
        """Convert exported VMDKs to qcow2 format."""
        vm_info = self.state.get_artifact(self.vm_name, "vm_info")
        exported_disks = self.state.get_artifact(self.vm_name, "exported_disks")

        if not exported_disks:
            raise RuntimeError(
                f"No exported disks found for VM '{self.vm_name}'. "
                "The EXPORT_DISK phase may not have completed successfully."
            )

        guest_id = vm_info["guest_id"] if vm_info else ""
        is_win = _is_windows(guest_id)

        # Determine the SSH client to use for conversion.
        # Priority: dedicated conversion host > Proxmox node.
        # When running on Windows there is no local qemu-img, so we always
        # run conversion remotely via SSH and upload the VMDK first.
        from pathlib import PurePosixPath
        conv_ssh = self._conversion_ssh or self._proxmox_ssh

        converted_disks = []
        for disk_info in exported_disks:
            vmdk_path = Path(disk_info["local_path"])

            # If the conversion SSH host is Proxmox (or any remote Linux),
            # upload the VMDK there first.  The converted qcow2 will live on
            # that host too so that `qm importdisk` can reach it directly.
            # IMPORTANT: always use PurePosixPath for remote paths so that
            # forward slashes are preserved even when running on Windows.
            if conv_ssh is not None:
                remote_dir  = PurePosixPath("/var/lib/vmigrate") / self.vm_name
                remote_vmdk = remote_dir / vmdk_path.name
                remote_qcow2 = remote_dir / vmdk_path.with_suffix(".qcow2").name

                self.logger.info(
                    "Uploading %s → %s:%s (%.0f MB)",
                    vmdk_path.name,
                    conv_ssh.host,
                    remote_vmdk,
                    vmdk_path.stat().st_size / 1024 / 1024,
                )
                conv_ssh.run(f"mkdir -p {remote_dir}")
                conv_ssh.put_file(vmdk_path, str(remote_vmdk))

                qcow2_path = remote_qcow2   # PurePosixPath — lives on the remote host
            else:
                qcow2_path = PurePosixPath(vmdk_path.with_suffix(".qcow2"))
                remote_vmdk = PurePosixPath(vmdk_path)
                remote_qcow2 = qcow2_path

            if is_win and conv_ssh is not None:
                # Windows VM: use virt-v2v for driver injection (needs conversion host)
                nic_bridge = "vmbr0"
                vm_info_obj = self.state.get_artifact(self.vm_name, "vm_info")
                if vm_info_obj and vm_info_obj.get("nics"):
                    first_pg = vm_info_obj["nics"][0]["portgroup"]
                    nm = self.config.get_network_mapping(first_pg)
                    if nm:
                        nic_bridge = nm.proxmox_bridge

                converter = VirtV2VConverter(
                    ssh=conv_ssh,
                    virtio_iso_path=self.config.migration.virtio_iso_path,
                )
                converted_path = converter.convert(
                    vmdk_path=Path(str(remote_vmdk)),
                    output_dir=Path(str(remote_dir)),
                    vm_name=self.vm_name,
                    network_bridge=nic_bridge,
                )
            else:
                # Linux VM (or Windows without virt-v2v): use qemu-img via SSH
                converter_qemu = QemuImgConverter(ssh=conv_ssh)
                converted_path = converter_qemu.convert(
                    vmdk_path=Path(str(remote_vmdk)),
                    output_path=Path(str(remote_qcow2)),
                    sparse=True,
                )

            # Always store as POSIX path (forward slashes) — the qcow2 lives
            # on a remote Linux host so backslashes from Windows Path break SSH cmds.
            qcow2_posix = converted_path.as_posix() if hasattr(converted_path, 'as_posix') else str(converted_path).replace("\\", "/")
            converted_disks.append(
                {
                    "label": disk_info["label"],
                    "qcow2_path": qcow2_posix,
                    "disk_key": disk_info.get("disk_key"),
                }
            )
            self.logger.info("Converted disk '%s' -> %s", disk_info["label"], converted_path)

        self.state.set_artifact(self.vm_name, "converted_disks", converted_disks)

    def _verify_disk(self) -> None:
        """Verify qcow2 image integrity with qemu-img check."""
        converted_disks = self.state.get_artifact(self.vm_name, "converted_disks")
        if not converted_disks:
            raise RuntimeError(
                f"No converted disks found for VM '{self.vm_name}'."
            )

        conv_ssh = self._conversion_ssh or self._proxmox_ssh
        for disk_info in converted_disks:
            path = Path(disk_info["qcow2_path"])
            self.logger.info("Verifying disk: %s", path.name)
            if conv_ssh:
                # Verify on the host where the qcow2 lives
                qemu = QemuImgConverter(ssh=conv_ssh)
                ok = qemu.check(path)
            else:
                ok = verify_qcow2(path)

            if not ok:
                raise RuntimeError(
                    f"Disk integrity check failed for '{path}'. "
                    "The qcow2 image may be corrupt. "
                    "Re-run the migration to re-export and re-convert."
                )
            self.logger.info("Disk '%s' verification PASSED", path.name)

    def _proxmox_vm_create(self) -> None:
        """Create the empty Proxmox VM shell."""
        assert self._proxmox_client is not None

        vm_info = self.state.get_artifact(self.vm_name, "vm_info")
        vmid = self._proxmox_client.get_next_vmid()

        creator = VMCreator(self._proxmox_client)
        creator.create_vm(
            vmid=vmid,
            node=self.vm_config.target_node,
            vm_info=vm_info,
            storage_map=self.config.storage_map,
            network_map=self.config.network_map,
        )
        self.state.set_artifact(self.vm_name, "proxmox_vmid", vmid)
        self.logger.info(
            "Proxmox VM created: vmid=%d name='%s'", vmid, self.vm_name
        )

    @retry(attempts=3, delay=30, exceptions=(Exception,))
    def _proxmox_disk_import(self) -> None:
        """Import converted disk images into Proxmox storage."""
        assert self._proxmox_client is not None
        assert self._proxmox_ssh is not None

        vmid = self.state.get_artifact(self.vm_name, "proxmox_vmid")
        converted_disks = self.state.get_artifact(self.vm_name, "converted_disks")
        vm_info = self.state.get_artifact(self.vm_name, "vm_info")

        # qm importdisk must run on the SAME node where the VM was created.
        # Resolve the target node to an IP; if it's the same as the API host
        # (or resolution fails) reuse the existing proxmox SSH connection.
        target_node = self.vm_config.target_node
        cluster_ips = getattr(self.config.proxmox, 'cluster_ips', [])
        node_ip = self._proxmox_client.get_node_ip(target_node, extra_candidates=cluster_ips)
        prx_user = self.config.proxmox.user.split("@")[0]
        api_host = self.config.proxmox.host

        # Treat same host (by IP or hostname) as no extra connection needed
        import socket as _sock
        def _same_host(a: str, b: str) -> bool:
            try:
                return _sock.gethostbyname(a) == _sock.gethostbyname(b)
            except Exception:
                return a == b

        if node_ip and not _same_host(node_ip, api_host):
            self.logger.info(
                "Target node '%s' IP %s differs from API host %s — "
                "opening direct SSH for qm importdisk.",
                target_node, node_ip, api_host,
            )
            node_ssh = SSHClient(
                host=node_ip,
                user=prx_user,
                password=self.config.proxmox.password,
            )
            node_ssh.connect()
        else:
            self.logger.info(
                "Target node '%s' is same host as API (%s) — reusing existing SSH.",
                target_node, api_host,
            )
            node_ssh = self._proxmox_ssh

        disk_mgr = DiskManager(self._proxmox_client, ssh=node_ssh)
        imported_disks = []

        for idx, disk_info in enumerate(converted_disks):
            qcow2_path = Path(disk_info["qcow2_path"])

            # Determine target Proxmox storage from the VMware disk info
            # Use first storage mapping as default
            storage = self.config.storage_map[0].proxmox_storage

            # Try to find the datastore for this disk in the VM info
            for vmdisk in vm_info.get("disks", []):
                if vmdisk["label"] == disk_info["label"] and vmdisk["datastore"]:
                    sm = self.config.get_storage_mapping(vmdisk["datastore"])
                    if sm:
                        storage = sm.proxmox_storage
                    break

            disk_id = disk_mgr.import_disk(
                vmid=int(vmid),
                node=self.vm_config.target_node,
                qcow2_path=qcow2_path,
                storage=storage,
            )
            disk_mgr.attach_disk(
                vmid=int(vmid),
                node=self.vm_config.target_node,
                disk_id=disk_id,
                controller="scsi",
                index=idx,
            )
            if idx == 0:
                disk_mgr.set_boot_order(
                    vmid=int(vmid),
                    node=self.vm_config.target_node,
                    boot_disk="scsi0",
                )
            imported_disks.append(
                {"label": disk_info["label"], "slot": f"scsi{idx}"}
            )

        self.state.set_artifact(self.vm_name, "imported_disks", imported_disks)

    def _driver_inject(self) -> None:
        """Inject VirtIO drivers for Windows VMs (handled by virt-v2v).

        For Linux VMs this phase is a no-op.  For Windows VMs, driver
        injection was already performed by virt-v2v during CONVERT_DISK.
        This phase validates that the conversion included drivers.
        """
        vm_info = self.state.get_artifact(self.vm_name, "vm_info")
        guest_id = vm_info.get("guest_id", "") if vm_info else ""

        if _is_windows(guest_id):
            self.logger.info(
                "Windows VM '%s': VirtIO drivers injected by virt-v2v during "
                "CONVERT_DISK phase. Driver inject phase complete.",
                self.vm_name,
            )
        else:
            self.logger.info(
                "Linux VM '%s': No driver injection needed.", self.vm_name
            )

    def _proxmox_network(self) -> None:
        """Configure virtual NICs on the Proxmox VM."""
        assert self._proxmox_client is not None

        vmid = self.state.get_artifact(self.vm_name, "proxmox_vmid")
        vm_info = self.state.get_artifact(self.vm_name, "vm_info")
        guest_id = vm_info.get("guest_id", "") if vm_info else ""

        net_mgr = NetworkManager(self._proxmox_client)

        for idx, nic in enumerate(vm_info.get("nics", [])):
            portgroup = nic["portgroup"]
            mapping = self.config.get_network_mapping(portgroup)
            if mapping is None:
                raise RuntimeError(
                    f"No network_map for portgroup '{portgroup}'. "
                    "This should have been caught in PREFLIGHT."
                )
            model = net_mgr._map_vm_nic_model(guest_id)
            net_mgr.add_nic(
                vmid=int(vmid),
                node=self.vm_config.target_node,
                index=idx,
                bridge=mapping.proxmox_bridge,
                model=model,
                mac=nic.get("mac") or None,
                vlan=mapping.vlan_tag,
            )

    def _proxmox_start(self) -> None:
        """Start the migrated VM on Proxmox."""
        assert self._proxmox_client is not None

        vmid = self.state.get_artifact(self.vm_name, "proxmox_vmid")
        api = self._proxmox_client.get_api()

        self.logger.info("Starting Proxmox VM vmid=%d", int(vmid))
        try:
            api.nodes(self.vm_config.target_node).qemu(int(vmid)).status.start.post()  # type: ignore[union-attr]
        except Exception as exc:
            raise RuntimeError(
                f"Failed to start Proxmox VM vmid={vmid}: {exc}"
            ) from exc
        self.logger.info("VM vmid=%d started", int(vmid))

    def _snapshot_remove(self) -> None:
        """Remove the VMware migration snapshot."""
        assert self._vmware_client is not None

        moref = self.state.get_artifact(self.vm_name, "snapshot_moref")
        if not moref:
            self.logger.info("No snapshot moref found; skipping snapshot removal.")
            return

        inventory = VMwareInventory(self._vmware_client)
        vm = inventory.find_vm(self.vm_name, self.config.vmware.datacenter)
        snap_mgr = SnapshotManager(self._vmware_client)
        snap_mgr.remove_snapshot(vm, str(moref))
        self.logger.info("VMware snapshot removed for VM '%s'", self.vm_name)

    def _agent_install(self) -> None:
        """Install the QEMU guest agent in the migrated VM."""
        assert self._proxmox_client is not None

        vmid = self.state.get_artifact(self.vm_name, "proxmox_vmid")
        vm_info = self.state.get_artifact(self.vm_name, "vm_info")
        guest_id = vm_info.get("guest_id", "") if vm_info else ""

        installer = AgentInstaller(self._proxmox_client)
        installer.enable_agent_config(int(vmid), self.vm_config.target_node)

        # Wait for the VM to boot and agent to come up
        agent_up = installer.wait_for_agent(int(vmid), self.vm_config.target_node)

        if not agent_up:
            self.logger.warning(
                "Guest agent not yet responsive for vmid=%d. "
                "Attempting manual installation via exec API.",
                int(vmid),
            )

        if _is_windows(guest_id):
            self.logger.info(
                "Windows VM: guest agent installation must be done manually "
                "via WinRM or by running the installer inside the guest."
            )
        else:
            # Detect Linux distro family from guest_id
            distro = self._detect_distro(guest_id)
            try:
                installer.install_linux(int(vmid), self.vm_config.target_node, distro)
            except Exception as exc:
                self.logger.warning(
                    "Could not auto-install guest agent on vmid=%d: %s. "
                    "Install manually inside the guest.",
                    int(vmid),
                    exc,
                )

        # Run post-migrate script if configured
        if self.vm_config.post_migrate_script:
            self._run_post_migrate_script()

    def _detect_distro(self, guest_id: str) -> str:
        """Guess the Linux distro family from the VMware guest ID."""
        g = guest_id.lower()
        if "rhel" in g or "centos" in g or "redhat" in g:
            return "rhel"
        if "fedora" in g:
            return "fedora"
        if "ubuntu" in g:
            return "ubuntu"
        if "debian" in g:
            return "debian"
        if "suse" in g or "sles" in g:
            return "suse"
        return "rhel"  # safe default for enterprise Linux

    def _run_post_migrate_script(self) -> None:
        """Execute the post-migration script if configured."""
        script = self.vm_config.post_migrate_script
        if not script:
            return
        self.logger.info("Running post-migrate script: %s", script)
        result = subprocess.run(
            [script],
            capture_output=True,
            text=True,
            timeout=300,
        )
        if result.returncode != 0:
            self.logger.warning(
                "Post-migrate script '%s' exited %d:\n%s",
                script,
                result.returncode,
                result.stderr,
            )
        else:
            self.logger.info("Post-migrate script completed successfully.")
