"""Batch CLI for managing large-scale VM migration batches.

Provides commands to split master VM lists into nightly batches,
generate batch configs, and manage batch runs.
"""

from pathlib import Path

import click
from rich.console import Console

console = Console()


@click.group()
def batch_cli() -> None:
    """Manage VM migration batches."""
    pass


@batch_cli.command("split")
@click.option(
    "--input",
    "-i",
    required=True,
    type=click.Path(exists=True),
    help="Master list file (JSON or newline-separated VM names).",
)
@click.option(
    "--batch-size",
    "-s",
    default=30,
    type=int,
    help="Target VMs per batch (default: 30).",
)
@click.option(
    "--output-dir",
    "-o",
    default="./batches",
    help="Output directory for batch files (default: ./batches).",
)
def split_batches(input: str, batch_size: int, output_dir: str) -> None:
    """Split a master VM list into batch files.

    Creates numbered batch files (batch_0.txt, batch_1.txt, etc.)
    with up to batch_size VMs each.

    \b
    Example:
      vmigrate batch split --input all-vms.txt --batch-size 30 --output-dir ./batches
    """
    from vmigrate.batch import load_vm_list_from_file, split_vms_into_batches

    input_path = Path(input)
    output_path = Path(output_dir)

    try:
        all_vms = load_vm_list_from_file(input_path)
        console.print(f"[cyan]Loaded {len(all_vms)} VMs from {input_path}[/cyan]")

        batches = split_vms_into_batches(all_vms, batch_size)
        console.print(f"[cyan]Split into {len(batches)} batches[/cyan]\n")

        output_path.mkdir(parents=True, exist_ok=True)

        for idx, batch in enumerate(batches):
            batch_file = output_path / f"batch_{idx}.txt"
            batch_file.write_text("\n".join(batch) + "\n", encoding="utf-8")
            console.print(
                f"  [green]batch_{idx}.txt[/green] ({len(batch)} VMs): "
                f"{batch[0]}...{batch[-1]}"
            )

        console.print(
            f"\n[bold green]Batch files created in {output_path}[/bold green]"
        )
    except Exception as exc:
        console.print(f"[bold red]Error:[/bold red] {exc}")
        raise click.Abort()


@batch_cli.command("generate-config")
@click.option(
    "--master-config",
    "-m",
    required=True,
    type=click.Path(exists=True),
    help="Master migration.yaml containing all VMs.",
)
@click.option(
    "--vm-file",
    "-v",
    required=True,
    type=click.Path(exists=True),
    help="Batch VM list file (newline-separated VM names).",
)
@click.option(
    "--output",
    "-o",
    required=True,
    help="Output config file path (e.g., batch_0.yaml).",
)
def generate_batch_config(master_config: str, vm_file: str, output: str) -> None:
    """Generate a batch-specific config from master config and VM list.

    \b
    Example:
      vmigrate batch generate-config \\
        --master-config config/migration.yaml \\
        --vm-file batches/batch_0.txt \\
        --output config/batch_0.yaml
    """
    from vmigrate.batch import generate_batch_config, load_vm_list_from_file

    try:
        master_path = Path(master_config)
        vm_file_path = Path(vm_file)
        output_path = Path(output)

        vm_names = load_vm_list_from_file(vm_file_path)
        console.print(f"[cyan]Loaded {len(vm_names)} VMs from {vm_file_path}[/cyan]")

        generate_batch_config(master_path, output_path, vm_names)
        console.print(
            f"[bold green]Batch config created: {output_path}[/bold green]"
        )
    except Exception as exc:
        console.print(f"[bold red]Error:[/bold red] {exc}")
        raise click.Abort()


if __name__ == "__main__":
    batch_cli()
