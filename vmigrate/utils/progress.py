"""Rich-based progress tracking for VM migrations.

Provides a ``MigrationProgress`` context manager that displays a live
multi-VM progress table using the Rich library.
"""

from __future__ import annotations

import threading
from typing import Optional

from rich.console import Console
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TaskID,
    TextColumn,
    TimeElapsedColumn,
)

# Total number of meaningful phases (excluding COMPLETED/FAILED)
_TOTAL_PHASES = 12

_CONSOLE = Console(stderr=True)


class MigrationProgress:
    """Live multi-VM migration progress display using Rich.

    Each VM is represented as a separate progress task with a spinner, phase
    label, progress bar, and elapsed time.

    Example::

        with MigrationProgress() as prog:
            prog.add_vm("web-server-01")
            prog.update("web-server-01", "EXPORT_DISK", advance=1)
            prog.complete("web-server-01")
    """

    def __init__(self) -> None:
        self._progress = Progress(
            SpinnerColumn(),
            TextColumn("[bold blue]{task.fields[vm_name]}[/bold blue]"),
            BarColumn(bar_width=30),
            MofNCompleteColumn(),
            TextColumn("[cyan]{task.fields[phase]}[/cyan]"),
            TimeElapsedColumn(),
            console=_CONSOLE,
            transient=False,
            refresh_per_second=4,
        )
        self._task_ids: dict[str, TaskID] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # VM management
    # ------------------------------------------------------------------

    def add_vm(self, vm_name: str) -> None:
        """Register a VM for progress tracking.

        Creates a new progress task row for ``vm_name``.  Safe to call before
        the progress display is started.

        Args:
            vm_name: The VM name to add.
        """
        with self._lock:
            if vm_name not in self._task_ids:
                task_id = self._progress.add_task(
                    vm_name,
                    total=_TOTAL_PHASES,
                    vm_name=vm_name,
                    phase="PENDING",
                )
                self._task_ids[vm_name] = task_id

    def update(self, vm_name: str, phase: str, advance: int = 1) -> None:
        """Update the phase label and advance the progress bar for a VM.

        Args:
            vm_name: The VM name to update.
            phase: Current phase name string (e.g. "EXPORT_DISK").
            advance: Number of steps to advance the progress bar.
        """
        with self._lock:
            task_id = self._task_ids.get(vm_name)
            if task_id is None:
                return
            self._progress.update(task_id, advance=advance, phase=phase)

    def complete(self, vm_name: str) -> None:
        """Mark a VM as fully completed (fills the progress bar to 100%).

        Args:
            vm_name: The VM name to mark complete.
        """
        with self._lock:
            task_id = self._task_ids.get(vm_name)
            if task_id is None:
                return
            self._progress.update(
                task_id,
                completed=_TOTAL_PHASES,
                phase="[green]COMPLETED[/green]",
            )

    def fail(self, vm_name: str, error: str) -> None:
        """Mark a VM as failed with an error message.

        Args:
            vm_name: The VM name that failed.
            error: Short error description to display.
        """
        with self._lock:
            task_id = self._task_ids.get(vm_name)
            if task_id is None:
                return
            short_error = error[:60] + "..." if len(error) > 60 else error
            self._progress.update(
                task_id,
                phase=f"[red]FAILED: {short_error}[/red]",
            )

    # ------------------------------------------------------------------
    # Context manager
    # ------------------------------------------------------------------

    def __enter__(self) -> "MigrationProgress":
        self._progress.start()
        return self

    def __exit__(self, exc_type: object, exc_val: object, exc_tb: object) -> None:
        self._progress.stop()
