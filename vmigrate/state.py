"""SQLite-backed state machine for tracking VM migration progress.

Each VM progresses through an ordered sequence of phases. The state database
persists phase transitions so that a failed migration can be resumed from the
last successful phase without repeating completed work.
"""

from __future__ import annotations

import json
import sqlite3
from enum import Enum
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class Phase(Enum):
    """Ordered migration phases.

    The integer value determines ordering; phases are executed in ascending
    value order. Do not reorder these without updating existing state DBs.
    """

    PREFLIGHT = 1
    SNAPSHOT_CREATE = 2
    EXPORT_DISK = 3
    CONVERT_DISK = 4
    VERIFY_DISK = 5
    PROXMOX_VM_CREATE = 6
    PROXMOX_DISK_IMPORT = 7
    DRIVER_INJECT = 8
    PROXMOX_NETWORK = 9
    PROXMOX_START = 10
    SNAPSHOT_REMOVE = 11
    AGENT_INSTALL = 12
    COMPLETED = 13
    FAILED = 99


class PhaseStatus(Enum):
    """Status of a single phase execution."""

    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


# Phases that are part of the normal forward progression (excludes COMPLETED/FAILED)
ORDERED_PHASES: list[Phase] = [
    Phase.PREFLIGHT,
    Phase.SNAPSHOT_CREATE,
    Phase.EXPORT_DISK,
    Phase.CONVERT_DISK,
    Phase.VERIFY_DISK,
    Phase.PROXMOX_VM_CREATE,
    Phase.PROXMOX_DISK_IMPORT,
    Phase.DRIVER_INJECT,
    Phase.PROXMOX_NETWORK,
    Phase.PROXMOX_START,
    Phase.SNAPSHOT_REMOVE,
    Phase.AGENT_INSTALL,
    Phase.COMPLETED,
]

# ---------------------------------------------------------------------------
# DDL
# ---------------------------------------------------------------------------

_CREATE_VM_TABLE = """
CREATE TABLE IF NOT EXISTS vm_state (
    vm_name     TEXT PRIMARY KEY,
    phase       TEXT NOT NULL,
    status      TEXT NOT NULL,
    error       TEXT,
    artifacts   TEXT NOT NULL DEFAULT '{}',
    updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

_CREATE_HISTORY_TABLE = """
CREATE TABLE IF NOT EXISTS vm_history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    vm_name     TEXT NOT NULL,
    phase       TEXT NOT NULL,
    status      TEXT NOT NULL,
    error       TEXT,
    recorded_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


# ---------------------------------------------------------------------------
# StateDB
# ---------------------------------------------------------------------------


class StateDB:
    """Persistent migration state store backed by SQLite.

    Uses WAL journal mode for concurrent read access while a migration is
    running.  All writes are serialised through the single connection held
    by this instance.
    """

    def __init__(self, db_path: Path) -> None:
        """Open (or create) the state database at ``db_path``.

        Args:
            db_path: Path to the SQLite file.  Parent directory must exist or
                be creatable.
        """
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db_path = db_path
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.execute("PRAGMA foreign_keys=ON;")
        self._create_tables()

    # ------------------------------------------------------------------
    # Setup
    # ------------------------------------------------------------------

    def _create_tables(self) -> None:
        """Create tables if they do not exist."""
        with self._conn:
            self._conn.execute(_CREATE_VM_TABLE)
            self._conn.execute(_CREATE_HISTORY_TABLE)

    # ------------------------------------------------------------------
    # VM lifecycle
    # ------------------------------------------------------------------

    def init_vm(self, vm_name: str) -> None:
        """Insert a new VM row at PREFLIGHT/PENDING if it does not exist.

        Safe to call multiple times; will not overwrite existing state.

        Args:
            vm_name: The VM name as it appears in vCenter.
        """
        with self._conn:
            self._conn.execute(
                """
                INSERT OR IGNORE INTO vm_state (vm_name, phase, status, artifacts)
                VALUES (?, ?, ?, '{}')
                """,
                (vm_name, Phase.PREFLIGHT.name, PhaseStatus.PENDING.value),
            )

    def get_vm_state(self, vm_name: str) -> dict:
        """Return the current state row for ``vm_name`` as a plain dict.

        Returns:
            dict with keys: vm_name, phase, status, error, artifacts (parsed
            dict), updated_at.  Empty dict if VM not found.
        """
        row = self._conn.execute(
            "SELECT * FROM vm_state WHERE vm_name = ?", (vm_name,)
        ).fetchone()
        if row is None:
            return {}
        result = dict(row)
        result["artifacts"] = json.loads(result.get("artifacts") or "{}")
        return result

    def transition(
        self,
        vm_name: str,
        phase: Phase,
        status: PhaseStatus,
        error: Optional[str] = None,
    ) -> None:
        """Update the phase and status for a VM and append a history record.

        Args:
            vm_name: VM name.
            phase: Target phase.
            status: New status for that phase.
            error: Optional error message (stored when status=FAILED).
        """
        with self._conn:
            self._conn.execute(
                """
                UPDATE vm_state
                SET phase = ?, status = ?, error = ?,
                    updated_at = datetime('now')
                WHERE vm_name = ?
                """,
                (phase.name, status.value, error, vm_name),
            )
            self._conn.execute(
                """
                INSERT INTO vm_history (vm_name, phase, status, error)
                VALUES (?, ?, ?, ?)
                """,
                (vm_name, phase.name, status.value, error),
            )

    # ------------------------------------------------------------------
    # Artifacts
    # ------------------------------------------------------------------

    def set_artifact(self, vm_name: str, key: str, value: object) -> None:
        """Store a key-value artifact for a VM (JSON serialised).

        Artifacts are used to pass data between phases, e.g. snapshot moref,
        Proxmox VMID, exported disk paths.

        Args:
            vm_name: VM name.
            key: Artifact key.
            value: JSON-serialisable value.
        """
        row = self._conn.execute(
            "SELECT artifacts FROM vm_state WHERE vm_name = ?", (vm_name,)
        ).fetchone()
        if row is None:
            raise KeyError(f"VM '{vm_name}' not found in state DB.")
        artifacts: dict = json.loads(row["artifacts"] or "{}")
        
        # Only write if the value actually changed
        if artifacts.get(key) != value:
            artifacts[key] = value
            with self._conn:
                self._conn.execute(
                    "UPDATE vm_state SET artifacts = ? WHERE vm_name = ?",
                    (json.dumps(artifacts), vm_name),
                )

    def get_artifact(self, vm_name: str, key: str) -> object:
        """Retrieve an artifact value for a VM.

        Args:
            vm_name: VM name.
            key: Artifact key.

        Returns:
            The stored value, or ``None`` if not found.
        """
        row = self._conn.execute(
            "SELECT artifacts FROM vm_state WHERE vm_name = ?", (vm_name,)
        ).fetchone()
        if row is None:
            return None
        artifacts: dict = json.loads(row["artifacts"] or "{}")
        return artifacts.get(key)

    # ------------------------------------------------------------------
    # Resume logic
    # ------------------------------------------------------------------

    def get_resume_phase(self, vm_name: str) -> Phase:
        """Return the next phase that should be executed for ``vm_name``.

        If the VM has never been initialised, returns Phase.PREFLIGHT.
        If the current phase succeeded, returns the following phase.
        If the current phase failed or is running (crash recovery), returns
        the current phase so it will be retried.

        Args:
            vm_name: VM name.

        Returns:
            The Phase to start (or resume) from.
        """
        state = self.get_vm_state(vm_name)
        if not state:
            return Phase.PREFLIGHT

        phase = Phase[state["phase"]]
        status = PhaseStatus(state["status"])

        if phase == Phase.COMPLETED:
            return Phase.COMPLETED
        if phase == Phase.FAILED:
            return Phase.FAILED

        if status == PhaseStatus.SUCCESS:
            # Advance to next phase
            current_idx = ORDERED_PHASES.index(phase)
            if current_idx + 1 < len(ORDERED_PHASES):
                return ORDERED_PHASES[current_idx + 1]
            return Phase.COMPLETED

        # PENDING, RUNNING (crash), or FAILED on current phase → retry it
        return phase

    def list_all(self) -> list[dict]:
        """Return state rows for all VMs as a list of dicts.

        Returns:
            List of dicts with the same shape as ``get_vm_state``.
        """
        rows = self._conn.execute(
            "SELECT * FROM vm_state ORDER BY vm_name"
        ).fetchall()
        result = []
        for row in rows:
            d = dict(row)
            d["artifacts"] = json.loads(d.get("artifacts") or "{}")
            result.append(d)
        return result

    def reset_to_checkpoint(self, vm_name: str) -> None:
        """Reset a FAILED VM to the last SUCCESS phase so it can be retried.

        Walks the history table backwards to find the last phase that
        completed successfully, then resets the current row to that phase
        with PENDING status so the orchestrator will re-run from there.

        Args:
            vm_name: VM name.

        Raises:
            ValueError: If the VM is not in FAILED state or has no history.
        """
        state = self.get_vm_state(vm_name)
        if not state:
            raise ValueError(f"VM '{vm_name}' not found in state DB.")

        if PhaseStatus(state["status"]) != PhaseStatus.FAILED:
            raise ValueError(
                f"VM '{vm_name}' is not in FAILED state "
                f"(current: {state['phase']}/{state['status']}). "
                "Only FAILED migrations can be reset to a checkpoint."
            )

        # Find the most recent SUCCESS in history
        row = self._conn.execute(
            """
            SELECT phase FROM vm_history
            WHERE vm_name = ? AND status = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (vm_name, PhaseStatus.SUCCESS.value),
        ).fetchone()

        if row is None:
            # No success at all - reset to PREFLIGHT
            reset_phase = Phase.PREFLIGHT
        else:
            reset_phase = Phase[row["phase"]]
            # Advance to the phase after the last success
            if reset_phase in ORDERED_PHASES:
                idx = ORDERED_PHASES.index(reset_phase)
                if idx + 1 < len(ORDERED_PHASES):
                    reset_phase = ORDERED_PHASES[idx + 1]

        with self._conn:
            self._conn.execute(
                """
                UPDATE vm_state
                SET phase = ?, status = ?, error = NULL,
                    updated_at = datetime('now')
                WHERE vm_name = ?
                """,
                (reset_phase.name, PhaseStatus.PENDING.value, vm_name),
            )
            self._conn.execute(
                """
                INSERT INTO vm_history (vm_name, phase, status)
                VALUES (?, ?, 'RESET')
                """,
                (vm_name, reset_phase.name),
            )

    def close(self) -> None:
        """Close the database connection."""
        self._conn.close()
