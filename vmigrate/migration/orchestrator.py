"""Migration orchestrator for vmigrate.

Schedules and runs migrations for multiple VMs concurrently using a process
pool, with dry-run support and rollback capability.
"""

from __future__ import annotations

import logging
from concurrent.futures import ProcessPoolExecutor, as_completed
from typing import Optional

from vmigrate.config import MigrationConfig, VMConfig
from vmigrate.logging_setup import get_root_logger
from vmigrate.state import Phase, PhaseStatus, StateDB

logger = logging.getLogger("vmigrate.orchestrator")


def _run_vm_migration(
    config: MigrationConfig,
    vm_config: VMConfig,
    state_db_path: str,
) -> tuple[str, bool]:
    """Top-level function for running a single VM migration in a subprocess.

    Must be module-level (not a lambda or method) for pickle compatibility
    with ProcessPoolExecutor.

    Args:
        config: Global migration config.
        vm_config: Per-VM config.
        state_db_path: Path string to the SQLite state DB.

    Returns:
        Tuple of (vm_name, success).
    """
    from pathlib import Path
    from vmigrate.migration.cold import ColdMigration
    from vmigrate.migration.live import LiveMigration
    from vmigrate.state import StateDB

    state = StateDB(Path(state_db_path))
    mode = vm_config.mode_override or config.migration.mode

    try:
        if mode == "live":
            migration = LiveMigration(config, vm_config, state)
        else:
            migration = ColdMigration(config, vm_config, state)

        success = migration.run()
        return vm_config.name, success
    except Exception as exc:
        logger.error("Unhandled error in VM migration '%s': %s", vm_config.name, exc)
        return vm_config.name, False
    finally:
        state.close()


class MigrationOrchestrator:
    """Coordinate parallel VM migrations with dry-run and rollback support.

    Uses a :class:`~concurrent.futures.ProcessPoolExecutor` to run up to
    ``config.migration.max_parallel`` VM migrations concurrently.

    Example::

        orchestrator = MigrationOrchestrator(config, state)
        orchestrator.run(vm_names=["web-01", "db-01"])
    """

    def __init__(self, config: MigrationConfig, state: StateDB) -> None:
        """Initialise the orchestrator.

        Args:
            config: Global :class:`MigrationConfig`.
            state: Shared :class:`StateDB` instance.
        """
        self.config = config
        self.state = state
        self.logger = get_root_logger()

    # ------------------------------------------------------------------
    # Main orchestration
    # ------------------------------------------------------------------

    def run(
        self,
        vm_names: Optional[list[str]] = None,
        dry_run: bool = False,
    ) -> dict[str, bool]:
        """Migrate a set of VMs, optionally in parallel.

        Args:
            vm_names: Specific VM names to migrate.  If ``None``, all VMs
                defined in the config are migrated.
            dry_run: Print the migration plan and exit without running.

        Returns:
            Dict mapping VM name to success flag.
        """
        # Resolve which VMs to migrate
        all_vm_configs = {vc.name: vc for vc in self.config.vms}
        if vm_names:
            missing = [n for n in vm_names if n not in all_vm_configs]
            if missing:
                raise ValueError(
                    f"The following VM names are not in the config: {missing}. "
                    "Check the 'vms' section in your config file."
                )
            target_vms = [all_vm_configs[n] for n in vm_names]
        else:
            target_vms = list(self.config.vms)

        if not target_vms:
            self.logger.warning("No VMs to migrate.")
            return {}

        if dry_run:
            self.print_dry_run_plan([vc.name for vc in target_vms])
            return {}

        # Initialise state rows
        for vc in target_vms:
            self.state.init_vm(vc.name)

        results: dict[str, bool] = {}
        max_workers = min(self.config.migration.max_parallel, len(target_vms))
        self.logger.info(
            "Starting migration: %d VM(s) with max_parallel=%d",
            len(target_vms),
            max_workers,
        )

        state_db_path = str(self.config.migration.state_db)

        if max_workers == 1:
            # Run sequentially to avoid subprocess overhead for a single VM
            for vc in target_vms:
                _, success = _run_vm_migration(self.config, vc, state_db_path)
                results[vc.name] = success
        else:
            with ProcessPoolExecutor(max_workers=max_workers) as executor:
                futures = {
                    executor.submit(
                        _run_vm_migration,
                        self.config,
                        vc,
                        state_db_path,
                    ): vc.name
                    for vc in target_vms
                }
                for future in as_completed(futures):
                    vm_name = futures[future]
                    try:
                        _, success = future.result()
                        results[vm_name] = success
                        status = "SUCCESS" if success else "FAILED"
                        self.logger.info(
                            "VM '%s' migration %s", vm_name, status
                        )
                    except Exception as exc:
                        self.logger.error(
                            "VM '%s' migration raised an exception: %s",
                            vm_name,
                            exc,
                        )
                        results[vm_name] = False

        succeeded = sum(1 for v in results.values() if v)
        failed = len(results) - succeeded
        self.logger.info(
            "Migration run complete: %d succeeded, %d failed",
            succeeded,
            failed,
        )
        return results

    # ------------------------------------------------------------------
    # Dry run
    # ------------------------------------------------------------------

    def print_dry_run_plan(self, vm_names: list[str]) -> None:
        """Print a summary of what would be migrated without executing.

        Args:
            vm_names: List of VM names that would be migrated.
        """
        from rich.console import Console
        from rich.table import Table

        console = Console()
        table = Table(title="Migration Plan (Dry Run)", show_lines=True)
        table.add_column("VM Name", style="bold cyan")
        table.add_column("Mode")
        table.add_column("Target Node")
        table.add_column("Post-Migrate Script")

        for name in vm_names:
            vc = next((v for v in self.config.vms if v.name == name), None)
            if vc is None:
                continue
            mode = vc.mode_override or self.config.migration.mode
            table.add_row(
                vc.name,
                mode,
                vc.target_node,
                vc.post_migrate_script or "-",
            )

        console.print(table)
        console.print(
            f"\n[bold]Source:[/bold] {self.config.vmware.host} "
            f"(datacenter: {self.config.vmware.datacenter})"
        )
        console.print(
            f"[bold]Destination:[/bold] {self.config.proxmox.host} "
            f"(node: {self.config.proxmox.node})"
        )
        console.print(
            f"\n[yellow]Dry run complete. Run without --dry-run to execute.[/yellow]"
        )

    # ------------------------------------------------------------------
    # Rollback
    # ------------------------------------------------------------------

    def rollback(self, vm_name: str) -> None:
        """Roll back a failed migration by cleaning up Proxmox resources.

        Deletes the Proxmox VM (using the stored VMID artifact) and removes
        the VMware migration snapshot if one exists.

        Args:
            vm_name: The VM name to roll back.

        Raises:
            ValueError: If the VM is not found in the state DB.
        """
        state = self.state.get_vm_state(vm_name)
        if not state:
            raise ValueError(
                f"VM '{vm_name}' not found in state DB. "
                "Cannot roll back a migration that was never started."
            )

        self.logger.info("Rolling back migration for VM '%s'...", vm_name)

        # Delete Proxmox VM if created
        vmid = state["artifacts"].get("proxmox_vmid")
        if vmid is not None:
            try:
                from vmigrate.proxmox.client import ProxmoxClient
                from vmigrate.proxmox.vm_create import VMCreator

                with ProxmoxClient(self.config.proxmox) as prox:
                    creator = VMCreator(prox)
                    # Find which node the VM was on
                    vm_config = next(
                        (vc for vc in self.config.vms if vc.name == vm_name), None
                    )
                    node = vm_config.target_node if vm_config else self.config.proxmox.node
                    creator.delete_vm(int(vmid), node)
                    self.logger.info(
                        "Deleted Proxmox VM vmid=%d during rollback", int(vmid)
                    )
            except Exception as exc:
                self.logger.error(
                    "Failed to delete Proxmox VM vmid=%s during rollback: %s",
                    vmid,
                    exc,
                )

        # Remove VMware snapshot if created
        moref = state["artifacts"].get("snapshot_moref")
        if moref:
            try:
                from vmigrate.vmware.client import VMwareClient
                from vmigrate.vmware.inventory import VMwareInventory
                from vmigrate.vmware.snapshot import SnapshotManager

                with VMwareClient(self.config.vmware) as vclient:
                    inv = VMwareInventory(vclient)
                    vm = inv.find_vm(vm_name, self.config.vmware.datacenter)
                    snap_mgr = SnapshotManager(vclient)
                    snap_mgr.remove_snapshot(vm, str(moref))
                    self.logger.info(
                        "Removed VMware snapshot (moref=%s) during rollback", moref
                    )
            except Exception as exc:
                self.logger.error(
                    "Failed to remove VMware snapshot (moref=%s) during rollback: %s",
                    moref,
                    exc,
                )

        # Reset state to FAILED so the operator knows what happened
        self.state.transition(
            vm_name,
            Phase.FAILED,
            PhaseStatus.FAILED,
            error="Rolled back by operator",
        )
        self.logger.info("Rollback complete for VM '%s'.", vm_name)
