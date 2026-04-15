"""Batch config generation for large-scale VM migrations.

Utilities to create batch-specific config files from a master VM list,
enabling you to split 15k VMs into nightly batches of 30 VMs, etc.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import yaml


def generate_batch_config(
    input_yaml: Path,
    output_yaml: Path,
    vm_names: list[str],
) -> None:
    """Create a batch config file containing only specified VMs.

    Args:
        input_yaml: Path to the master migration.yaml containing all VMs.
        output_yaml: Path to write the batch config to.
        vm_names: List of VM names to include in this batch.

    Raises:
        FileNotFoundError: If input_yaml does not exist.
        ValueError: If any VM name is not found in the input config.
    """
    with input_yaml.open("r", encoding="utf-8") as fh:
        config = yaml.safe_load(fh)

    if not config or "vms" not in config:
        raise ValueError(f"No 'vms' section found in {input_yaml}")

    # Map existing VMs by name
    existing_vms = {vm["name"]: vm for vm in config.get("vms", [])}

    # Validate all requested VMs exist
    missing = [n for n in vm_names if n not in existing_vms]
    if missing:
        raise ValueError(
            f"The following VMs are not in the config: {missing}\n"
            f"Available VMs: {list(existing_vms.keys())}"
        )

    # Build batch config with only the requested VMs
    batch_config = config.copy()
    batch_config["vms"] = [existing_vms[name] for name in vm_names]

    output_yaml.parent.mkdir(parents=True, exist_ok=True)
    with output_yaml.open("w", encoding="utf-8") as fh:
        yaml.dump(batch_config, fh, default_flow_style=False, sort_keys=False)

    print(f"Batch config created: {output_yaml}")
    print(f"  VMs: {len(batch_config['vms'])}")


def load_vm_list_from_file(file_path: Path) -> list[str]:
    """Load a list of VM names from a JSON or text file.

    Args:
        file_path: Path to the file. Supports:
            - JSON: list of strings ["vm1", "vm2"]
            - Text: newline-separated VM names

    Returns:
        List of VM names.

    Raises:
        FileNotFoundError: If the file does not exist.
        ValueError: If the file format is invalid.
    """
    if not file_path.exists():
        raise FileNotFoundError(f"File not found: {file_path}")

    content = file_path.read_text(encoding="utf-8").strip()

    # Try JSON first
    try:
        data = json.loads(content)
        if isinstance(data, list) and all(isinstance(x, str) for x in data):
            return data
    except json.JSONDecodeError:
        pass

    # Fall back to newline-separated
    lines = [line.strip() for line in content.splitlines() if line.strip()]
    if lines:
        return lines

    raise ValueError(f"Could not parse VM list from {file_path}")


def split_vms_into_batches(
    vm_names: list[str],
    batch_size: int,
) -> list[list[str]]:
    """Split a list of VM names into batches.

    Args:
        vm_names: List of all VM names.
        batch_size: Target size per batch (last batch may be smaller).

    Returns:
        List of batches, each containing up to batch_size VM names.
    """
    batches = []
    for i in range(0, len(vm_names), batch_size):
        batches.append(vm_names[i : i + batch_size])
    return batches
