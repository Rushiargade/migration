"""Performance monitoring and metrics for VM migrations.

Tracks timing, throughput, and resource usage per VM and per batch.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger("vmigrate.metrics")


@dataclass
class PhaseMetrics:
    """Timing metrics for a single migration phase."""

    phase: str
    start_time: float
    end_time: Optional[float] = None
    duration_seconds: float = 0.0
    status: str = "PENDING"
    error: Optional[str] = None

    def complete(self, status: str = "SUCCESS", error: Optional[str] = None) -> None:
        """Mark the phase as complete."""
        self.end_time = time.time()
        self.duration_seconds = self.end_time - self.start_time
        self.status = status
        self.error = error


@dataclass
class VMMetrics:
    """Aggregated metrics for a single VM migration."""

    vm_name: str
    start_time: float
    end_time: Optional[float] = None
    status: str = "RUNNING"
    total_duration_seconds: float = 0.0
    phases: dict[str, PhaseMetrics] = None

    def __post_init__(self):
        if self.phases is None:
            self.phases = {}

    def complete(self, status: str = "SUCCESS") -> None:
        """Mark the VM migration as complete."""
        self.end_time = time.time()
        self.total_duration_seconds = self.end_time - self.start_time
        self.status = status

    def to_dict(self) -> dict:
        """Convert to JSON-serializable dict."""
        result = asdict(self)
        result["phases"] = {
            k: asdict(v) for k, v in self.phases.items()
        }
        return result


class MetricsCollector:
    """Collect and report migration metrics."""

    def __init__(self, output_dir: Optional[Path] = None) -> None:
        """Initialize the collector.

        Args:
            output_dir: Optional directory to write JSON metrics files.
        """
        self._output_dir = output_dir
        self._vms: dict[str, VMMetrics] = {}
        self._batch_start = time.time()

    def start_vm(self, vm_name: str) -> None:
        """Record the start of a VM migration."""
        self._vms[vm_name] = VMMetrics(
            vm_name=vm_name,
            start_time=time.time(),
        )
        logger.debug("Started tracking metrics for VM '%s'", vm_name)

    def start_phase(self, vm_name: str, phase: str) -> None:
        """Record the start of a phase for a VM."""
        if vm_name not in self._vms:
            self.start_vm(vm_name)

        self._vms[vm_name].phases[phase] = PhaseMetrics(
            phase=phase,
            start_time=time.time(),
        )

    def complete_phase(
        self,
        vm_name: str,
        phase: str,
        status: str = "SUCCESS",
        error: Optional[str] = None,
    ) -> None:
        """Record the completion of a phase."""
        if vm_name not in self._vms:
            return

        if phase in self._vms[vm_name].phases:
            self._vms[vm_name].phases[phase].complete(status=status, error=error)
            duration = self._vms[vm_name].phases[phase].duration_seconds
            logger.debug(
                "Phase '%s' for VM '%s' completed in %.1f seconds",
                phase,
                vm_name,
                duration,
            )

    def complete_vm(self, vm_name: str, status: str = "SUCCESS") -> None:
        """Record the completion of a VM migration."""
        if vm_name in self._vms:
            self._vms[vm_name].complete(status=status)
            duration = self._vms[vm_name].total_duration_seconds
            logger.info(
                "VM '%s' migration completed in %.1f seconds (status=%s)",
                vm_name,
                duration,
                status,
            )

    def get_batch_summary(self) -> dict:
        """Get summary metrics for the entire batch."""
        batch_duration = time.time() - self._batch_start
        successes = sum(1 for v in self._vms.values() if v.status == "SUCCESS")
        failures = sum(1 for v in self._vms.values() if v.status != "SUCCESS")

        total_vm_time = sum(v.total_duration_seconds for v in self._vms.values())
        avg_vm_time = total_vm_time / len(self._vms) if self._vms else 0

        return {
            "batch_duration_seconds": batch_duration,
            "batch_start": datetime.fromtimestamp(self._batch_start).isoformat(),
            "total_vms": len(self._vms),
            "successful_vms": successes,
            "failed_vms": failures,
            "average_vm_duration_seconds": avg_vm_time,
            "total_vm_duration_seconds": total_vm_time,
            "parallelism_efficiency": (
                total_vm_time / batch_duration if batch_duration > 0 else 0
            ),
        }

    def print_summary(self) -> None:
        """Print a formatted summary of the batch metrics."""
        summary = self.get_batch_summary()
        logger.info("=" * 60)
        logger.info("BATCH MIGRATION METRICS")
        logger.info("=" * 60)
        logger.info("Total VMs: %d (success: %d, failed: %d)",
                    summary["total_vms"],
                    summary["successful_vms"],
                    summary["failed_vms"])
        logger.info("Batch duration: %.1f seconds (%.1f minutes)",
                    summary["batch_duration_seconds"],
                    summary["batch_duration_seconds"] / 60)
        logger.info("Average VM duration: %.1f seconds",
                    summary["average_vm_duration_seconds"])
        logger.info("Parallelism efficiency: %.1f%%",
                    summary["parallelism_efficiency"] * 100)
        logger.info("=" * 60)

    def export_json(self, path: Path) -> None:
        """Export all metrics to a JSON file.

        Args:
            path: Output JSON file path.
        """
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "summary": self.get_batch_summary(),
            "vms": [v.to_dict() for v in self._vms.values()],
        }
        with path.open("w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
        logger.info("Metrics exported to %s", path)
