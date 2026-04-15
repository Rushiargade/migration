"""Command-line interface for vmigrate.

Provides commands for validating configuration, running preflight checks,
executing migrations, monitoring status, and rolling back failed migrations.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

import click
from rich.console import Console
from rich.table import Table
from rich import box

console = Console()

DEFAULT_CONFIG = "./config/migration.yaml"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_config_or_exit(config_path: str):
    """Load and validate config, exiting with an error on failure."""
    from vmigrate.config import load_config, ConfigError

    path = Path(config_path)
    try:
        return load_config(path)
    except FileNotFoundError as exc:
        console.print(f"[bold red]Error:[/bold red] {exc}")
        sys.exit(1)
    except ConfigError as exc:
        console.print(f"[bold red]Config error:[/bold red] {exc}")
        sys.exit(1)


def _get_state_db(config):
    """Open the StateDB for the given config."""
    from vmigrate.state import StateDB
    return StateDB(config.migration.state_db)


def _phase_color(phase: str, status: str) -> str:
    """Return a Rich markup string for a phase/status combination."""
    if status == "SUCCESS":
        return f"[green]{phase}[/green]"
    if status == "FAILED":
        return f"[bold red]{phase}[/bold red]"
    if status == "RUNNING":
        return f"[yellow]{phase}[/yellow]"
    return f"[dim]{phase}[/dim]"


def _status_badge(status: str) -> str:
    """Return a coloured status badge string."""
    badges = {
        "SUCCESS": "[bold green]SUCCESS[/bold green]",
        "FAILED":  "[bold red]FAILED[/bold red]",
        "RUNNING": "[bold yellow]RUNNING[/bold yellow]",
        "PENDING": "[dim]PENDING[/dim]",
    }
    return badges.get(status, status)


# ---------------------------------------------------------------------------
# CLI group
# ---------------------------------------------------------------------------


@click.group()
@click.version_option(package_name="vmigrate")
def cli() -> None:
    """vmigrate - Migrate VMs from VMware vSphere 7 to Proxmox VE.

    \b
    Quick start:
      1. Copy config/migration.example.yaml to config/migration.yaml
      2. Fill in your vCenter and Proxmox credentials
      3. Run: vmigrate validate-config
      4. Run: vmigrate preflight
      5. Run: vmigrate migrate --all
    """


# ---------------------------------------------------------------------------
# validate-config
# ---------------------------------------------------------------------------


@cli.command("validate-config")
@click.option(
    "--config",
    "-c",
    default=DEFAULT_CONFIG,
    show_default=True,
    help="Path to migration YAML config file.",
)
def validate_config(config: str) -> None:
    """Validate the migration config file and report any errors.

    Checks YAML syntax, required fields, environment variable references,
    and cross-field consistency.
    """
    from vmigrate.config import load_config, ConfigError

    path = Path(config)
    console.print(f"Validating config: [cyan]{path}[/cyan]")
    try:
        cfg = load_config(path)
        console.print("[bold green]Config is valid.[/bold green]")
        console.print(
            f"  VMware: [cyan]{cfg.vmware.host}[/cyan] "
            f"(datacenter: {cfg.vmware.datacenter})"
        )
        console.print(
            f"  Proxmox: [cyan]{cfg.proxmox.host}[/cyan] "
            f"(node: {cfg.proxmox.node})"
        )
        console.print(
            f"  Mode: {cfg.migration.mode} | "
            f"Max parallel: {cfg.migration.max_parallel}"
        )
        console.print(f"  VMs to migrate: {len(cfg.vms)}")
        for vm in cfg.vms:
            mode = vm.mode_override or cfg.migration.mode
            console.print(
                f"    - [bold]{vm.name}[/bold] "
                f"(mode={mode}, node={vm.target_node})"
            )
    except FileNotFoundError as exc:
        console.print(f"[bold red]Error:[/bold red] {exc}")
        sys.exit(1)
    except ConfigError as exc:
        console.print(f"[bold red]Config error:[/bold red] {exc}")
        sys.exit(1)


# ---------------------------------------------------------------------------
# preflight
# ---------------------------------------------------------------------------


@cli.command("preflight")
@click.option("--config", "-c", default=DEFAULT_CONFIG, show_default=True)
@click.argument("vm_name", required=False)
def preflight(config: str, vm_name: Optional[str]) -> None:
    """Run preflight checks against VMware and Proxmox.

    Verifies connectivity, locates VMs, checks storage and network mappings,
    and ensures all required tools are available.

    Optionally restrict checks to a single VM_NAME.
    """
    cfg = _load_config_or_exit(config)

    vms_to_check = cfg.vms
    if vm_name:
        vms_to_check = [v for v in cfg.vms if v.name == vm_name]
        if not vms_to_check:
            console.print(
                f"[bold red]Error:[/bold red] VM '{vm_name}' not found in config."
            )
            sys.exit(1)

    from vmigrate.vmware.client import VMwareClient
    from vmigrate.vmware.inventory import VMwareInventory
    from vmigrate.proxmox.client import ProxmoxClient

    all_passed = True

    def check(label: str, fn):
        nonlocal all_passed
        try:
            fn()
            console.print(f"  [green]PASS[/green] {label}")
        except Exception as exc:
            console.print(f"  [bold red]FAIL[/bold red] {label}: {exc}")
            all_passed = False

    console.print("\n[bold]== VMware Connectivity ==[/bold]")
    vmware_ok = False
    try:
        vmware_client = VMwareClient(cfg.vmware)
        vmware_client.connect()
        console.print(f"  [green]PASS[/green] Connected to {cfg.vmware.host}")
        vmware_ok = True
    except Exception as exc:
        console.print(
            f"  [bold red]FAIL[/bold red] Cannot connect to VMware {cfg.vmware.host}: {exc}"
        )
        all_passed = False

    console.print("\n[bold]== Proxmox Connectivity ==[/bold]")
    proxmox_ok = False
    try:
        proxmox_client = ProxmoxClient(cfg.proxmox)
        proxmox_client.connect()
        console.print(f"  [green]PASS[/green] Connected to {cfg.proxmox.host}")
        proxmox_ok = True
    except Exception as exc:
        console.print(
            f"  [bold red]FAIL[/bold red] Cannot connect to Proxmox {cfg.proxmox.host}: {exc}"
        )
        all_passed = False

    if proxmox_ok:
        check(
            f"Proxmox node '{cfg.proxmox.node}' is online",
            lambda: (
                None
                if proxmox_client.verify_node(cfg.proxmox.node)
                else (_ for _ in ()).throw(
                    RuntimeError(f"Node '{cfg.proxmox.node}' is not online")
                )
            ),
        )

    if vmware_ok:
        console.print("\n[bold]== VM Checks ==[/bold]")
        inv = VMwareInventory(vmware_client)
        for vc in vms_to_check:
            console.print(f"\n  VM: [bold cyan]{vc.name}[/bold cyan]")
            try:
                vm = inv.find_vm(vc.name, cfg.vmware.datacenter)
                vm_info = inv.get_vm_info(vm)
                console.print(
                    f"    [green]PASS[/green] Found "
                    f"(cpus={vm_info['num_cpus']}, "
                    f"mem={vm_info['memory_mb']}MB, "
                    f"disks={len(vm_info['disks'])}, "
                    f"nics={len(vm_info['nics'])})"
                )
                # Check storage mappings
                for disk in vm_info["disks"]:
                    ds = disk["datastore"]
                    if cfg.get_storage_mapping(ds):
                        console.print(
                            f"    [green]PASS[/green] Storage map: '{ds}'"
                        )
                    else:
                        console.print(
                            f"    [bold red]FAIL[/bold red] No storage_map for "
                            f"datastore '{ds}'"
                        )
                        all_passed = False
                # Check network mappings
                for nic in vm_info["nics"]:
                    pg = nic["portgroup"]
                    if cfg.get_network_mapping(pg):
                        console.print(
                            f"    [green]PASS[/green] Network map: '{pg}'"
                        )
                    else:
                        console.print(
                            f"    [bold red]FAIL[/bold red] No network_map for "
                            f"portgroup '{pg}'"
                        )
                        all_passed = False
            except Exception as exc:
                console.print(
                    f"    [bold red]FAIL[/bold red] {exc}"
                )
                all_passed = False

        try:
            vmware_client.disconnect()
        except Exception:
            pass

    console.print()
    if all_passed:
        console.print("[bold green]All preflight checks PASSED.[/bold green]")
    else:
        console.print(
            "[bold red]One or more preflight checks FAILED. "
            "Fix the issues above before migrating.[/bold red]"
        )
        sys.exit(1)


# ---------------------------------------------------------------------------
# migrate
# ---------------------------------------------------------------------------


@cli.command("migrate")
@click.option("--config", "-c", default=DEFAULT_CONFIG, show_default=True)
@click.option("--vm", "vm_name", default=None, help="Migrate a single VM by name.")
@click.option("--all", "migrate_all", is_flag=True, help="Migrate all VMs in config.")
@click.option("--dry-run", is_flag=True, help="Print plan without executing.")
@click.option(
    "--log-level",
    default="INFO",
    show_default=True,
    type=click.Choice(["DEBUG", "INFO", "WARNING", "ERROR"], case_sensitive=False),
    help="Logging verbosity.",
)
def migrate(
    config: str,
    vm_name: Optional[str],
    migrate_all: bool,
    dry_run: bool,
    log_level: str,
) -> None:
    """Execute VM migration(s) from VMware to Proxmox.

    Either --vm NAME or --all must be specified.

    \b
    Examples:
      vmigrate migrate --all
      vmigrate migrate --vm web-server-01
      vmigrate migrate --all --dry-run
      vmigrate migrate --vm db-01 --log-level DEBUG
    """
    if not vm_name and not migrate_all:
        console.print(
            "[bold red]Error:[/bold red] Specify --vm NAME or --all."
        )
        sys.exit(1)

    cfg = _load_config_or_exit(config)
    state = _get_state_db(cfg)

    vm_names = [vm_name] if vm_name else None

    from vmigrate.migration.orchestrator import MigrationOrchestrator

    orchestrator = MigrationOrchestrator(cfg, state)
    try:
        results = orchestrator.run(vm_names=vm_names, dry_run=dry_run)
    finally:
        state.close()

    if dry_run:
        return

    # Print summary
    console.print("\n[bold]== Migration Summary ==[/bold]")
    for name, success in results.items():
        badge = "[bold green]SUCCESS[/bold green]" if success else "[bold red]FAILED[/bold red]"
        console.print(f"  {name}: {badge}")

    failed = [n for n, ok in results.items() if not ok]
    if failed:
        console.print(
            f"\n[bold red]{len(failed)} VM(s) failed:[/bold red] {', '.join(failed)}"
        )
        console.print(
            "Run [cyan]vmigrate status --config ...[/cyan] for details, "
            "then [cyan]vmigrate retry --config ... VM_NAME[/cyan] to resume."
        )
        sys.exit(1)


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


@cli.command("status")
@click.option("--config", "-c", default=DEFAULT_CONFIG, show_default=True)
@click.argument("vm_name", required=False)
def status(config: str, vm_name: Optional[str]) -> None:
    """Show migration status for all VMs or a specific VM.

    Displays current phase, status, last error, and stored artifacts from
    the SQLite state database.

    \b
    Examples:
      vmigrate status
      vmigrate status web-server-01
    """
    cfg = _load_config_or_exit(config)
    state = _get_state_db(cfg)

    try:
        rows = state.list_all()
    finally:
        state.close()

    if vm_name:
        rows = [r for r in rows if r["vm_name"] == vm_name]
        if not rows:
            console.print(
                f"[bold red]Error:[/bold red] VM '{vm_name}' not found in state DB. "
                "Run 'vmigrate migrate' first."
            )
            sys.exit(1)

    if not rows:
        console.print(
            "[dim]No migration state found. Run 'vmigrate migrate' to start.[/dim]"
        )
        return

    table = Table(
        title="VM Migration Status",
        show_lines=True,
        box=box.ROUNDED,
    )
    table.add_column("VM Name", style="bold cyan", no_wrap=True)
    table.add_column("Phase", no_wrap=True)
    table.add_column("Status", no_wrap=True)
    table.add_column("Updated")
    table.add_column("Error", max_width=50)

    for row in rows:
        phase_str = _phase_color(row["phase"], row["status"])
        status_str = _status_badge(row["status"])
        error_str = row.get("error") or ""
        if len(error_str) > 80:
            error_str = error_str[:77] + "..."
        table.add_row(
            row["vm_name"],
            phase_str,
            status_str,
            row.get("updated_at", ""),
            error_str,
        )

    console.print(table)

    # Show artifacts for a single VM if requested
    if vm_name and rows:
        artifacts = rows[0].get("artifacts", {})
        if artifacts:
            console.print("\n[bold]Artifacts:[/bold]")
            for k, v in artifacts.items():
                if k not in ("vm_info",):  # skip large nested dicts
                    console.print(f"  [cyan]{k}[/cyan]: {v}")


# ---------------------------------------------------------------------------
# retry
# ---------------------------------------------------------------------------


@cli.command("retry")
@click.option("--config", "-c", default=DEFAULT_CONFIG, show_default=True)
@click.argument("vm_name")
def retry_cmd(config: str, vm_name: str) -> None:
    """Resume a failed migration from the last successful phase.

    Resets the VM state to the phase after the last SUCCESS, then re-runs
    the migration.

    \b
    Example:
      vmigrate retry web-server-01
    """
    cfg = _load_config_or_exit(config)
    state = _get_state_db(cfg)

    try:
        vm_state = state.get_vm_state(vm_name)
        if not vm_state:
            console.print(
                f"[bold red]Error:[/bold red] VM '{vm_name}' not found in state DB."
            )
            sys.exit(1)

        from vmigrate.state import PhaseStatus
        if PhaseStatus(vm_state["status"]) != PhaseStatus.FAILED:
            console.print(
                f"[bold red]Error:[/bold red] VM '{vm_name}' is not in FAILED state "
                f"(current: {vm_state['phase']}/{vm_state['status']}). "
                "Only FAILED migrations can be retried."
            )
            sys.exit(1)

        state.reset_to_checkpoint(vm_name)
        console.print(
            f"[green]State reset for VM '{vm_name}'.[/green] "
            "Resuming migration..."
        )
    finally:
        state.close()

    # Re-run the migration
    from click.testing import CliRunner
    # Invoke migrate programmatically
    state2 = _get_state_db(cfg)
    from vmigrate.migration.orchestrator import MigrationOrchestrator

    orchestrator = MigrationOrchestrator(cfg, state2)
    try:
        results = orchestrator.run(vm_names=[vm_name])
    finally:
        state2.close()

    success = results.get(vm_name, False)
    if success:
        console.print(f"[bold green]Migration for '{vm_name}' COMPLETED.[/bold green]")
    else:
        console.print(
            f"[bold red]Migration for '{vm_name}' FAILED again.[/bold red] "
            "Run 'vmigrate status' for details."
        )
        sys.exit(1)


# ---------------------------------------------------------------------------
# rollback
# ---------------------------------------------------------------------------


@cli.command("rollback")
@click.option("--config", "-c", default=DEFAULT_CONFIG, show_default=True)
@click.argument("vm_name")
def rollback(config: str, vm_name: str) -> None:
    """Roll back a failed migration and clean up created resources.

    Deletes the Proxmox VM (if created) and removes the VMware migration
    snapshot.  The VMware source VM is not affected.

    \b
    Example:
      vmigrate rollback web-server-01
    """
    cfg = _load_config_or_exit(config)
    state = _get_state_db(cfg)

    try:
        vm_state = state.get_vm_state(vm_name)
        if not vm_state:
            console.print(
                f"[bold red]Error:[/bold red] VM '{vm_name}' not found in state DB."
            )
            sys.exit(1)

        console.print(
            f"[yellow]Rolling back migration for VM '{vm_name}'...[/yellow]"
        )

        from vmigrate.migration.orchestrator import MigrationOrchestrator

        orchestrator = MigrationOrchestrator(cfg, state)
        orchestrator.rollback(vm_name)
        console.print(
            f"[bold green]Rollback complete for '{vm_name}'.[/bold green]"
        )
    except Exception as exc:
        console.print(f"[bold red]Rollback failed:[/bold red] {exc}")
        sys.exit(1)
    finally:
        state.close()


# ---------------------------------------------------------------------------
# list-vms
# ---------------------------------------------------------------------------


@cli.command("list-vms")
@click.option("--config", "-c", default=DEFAULT_CONFIG, show_default=True)
def list_vms(config: str) -> None:
    """List all VMs found in the VMware vSphere datacenter.

    Connects to vCenter and lists every VM with its CPU, memory, and power
    state.  Useful for discovering VM names to add to your config.

    \b
    Example:
      vmigrate list-vms
    """
    cfg = _load_config_or_exit(config)

    from vmigrate.vmware.client import VMwareClient
    from vmigrate.vmware.inventory import VMwareInventory

    console.print(
        f"Connecting to [cyan]{cfg.vmware.host}[/cyan] "
        f"(datacenter: {cfg.vmware.datacenter})..."
    )
    try:
        with VMwareClient(cfg.vmware) as client:
            inv = VMwareInventory(client)
            vms = inv.list_vms(cfg.vmware.datacenter)
    except Exception as exc:
        console.print(f"[bold red]Error:[/bold red] {exc}")
        sys.exit(1)

    if not vms:
        console.print("[dim]No VMs found in this datacenter.[/dim]")
        return

    table = Table(
        title=f"VMs in {cfg.vmware.datacenter}",
        show_lines=True,
        box=box.ROUNDED,
    )
    table.add_column("Name", style="bold cyan")
    table.add_column("Guest OS")
    table.add_column("CPUs", justify="right")
    table.add_column("Memory (MB)", justify="right")
    table.add_column("Power State")

    for vm in vms:
        power = vm["power_state"]
        power_str = (
            "[green]poweredOn[/green]"
            if "On" in power
            else "[dim]poweredOff[/dim]"
        )
        table.add_row(
            vm["name"],
            vm["guest_id"],
            str(vm["num_cpus"]),
            str(vm["memory_mb"]),
            power_str,
        )

    console.print(table)
    console.print(f"\nTotal: {len(vms)} VMs")


# ---------------------------------------------------------------------------
# serve
# ---------------------------------------------------------------------------


@cli.command("serve")
@click.option("--host", default="0.0.0.0", show_default=True, help="Bind host.")
@click.option("--port", default=8080, show_default=True, type=int, help="Bind port.")
@click.option(
    "--config",
    "-c",
    default=None,
    help="Optional migration YAML to pre-load in the UI.",
)
def serve(host: str, port: int, config: Optional[str]) -> None:
    """Launch the vmigrate Web UI.

    Opens a browser-based dashboard for configuring connections, selecting
    VMs, and monitoring migration progress.

    \b
    Example:
      vmigrate serve
      vmigrate serve --port 9090
    """
    try:
        import uvicorn
    except ImportError:
        console.print(
            "[bold red]Error:[/bold red] uvicorn is not installed.\n"
            "Install web dependencies: [cyan]pip install 'vmigrate[web]'[/cyan]"
        )
        sys.exit(1)

    try:
        from vmigrate.web.app import create_app
    except ImportError as exc:
        console.print(f"[bold red]Error:[/bold red] Cannot import web app: {exc}")
        sys.exit(1)

    app = create_app(config_path=config)

    console.print(f"[bold green]vmigrate Web UI[/bold green] starting on [cyan]http://{host}:{port}[/cyan]")
    console.print("Press [bold]Ctrl+C[/bold] to stop.\n")

    uvicorn.run(app, host=host, port=port, log_level="warning")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    cli()
