"""Logging configuration for vmigrate.

Sets up a dual-handler logger: a plain-text file handler per VM and a Rich
console handler with coloured phase labels.
"""

from __future__ import annotations

import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

from rich.console import Console
from rich.logging import RichHandler


# Module-level Rich console (stderr so it does not pollute piped output)
_CONSOLE = Console(stderr=True)


def setup_logging(
    vm_name: str,
    work_dir: Path,
    level: str = "DEBUG",
) -> logging.Logger:
    """Configure and return a logger for a specific VM migration.

    Creates two handlers:
    - A ``FileHandler`` writing plain-text structured lines to
      ``{work_dir}/logs/{vm_name}_{timestamp}.log``.
    - A ``RichHandler`` writing coloured output to stderr.

    The logger is namespaced as ``vmigrate.{vm_name}`` so that different VM
    loggers do not interfere with each other.

    Args:
        vm_name: The VM name; used for the log file name and logger namespace.
        work_dir: Root working directory.  A ``logs/`` sub-directory is
            created automatically.
        level: Logging level string, e.g. "DEBUG", "INFO", "WARNING".

    Returns:
        A configured :class:`logging.Logger` instance.
    """
    log_dir = work_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_file = log_dir / f"{vm_name}_{timestamp}.log"

    numeric_level = getattr(logging, level.upper(), logging.INFO)

    logger_name = f"vmigrate.{vm_name}"
    logger = logging.getLogger(logger_name)
    logger.setLevel(numeric_level)

    # Avoid adding duplicate handlers when called multiple times (e.g. in tests)
    if logger.handlers:
        logger.handlers.clear()

    # --- File handler (plain text) ---
    file_formatter = logging.Formatter(
        fmt="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    file_handler = logging.FileHandler(str(log_file), encoding="utf-8")
    file_handler.setLevel(numeric_level)
    file_handler.setFormatter(file_formatter)
    logger.addHandler(file_handler)

    # --- Rich console handler ---
    rich_handler = RichHandler(
        console=_CONSOLE,
        rich_tracebacks=True,
        show_path=False,
        markup=True,
    )
    rich_handler.setLevel(numeric_level)
    logger.addHandler(rich_handler)

    logger.propagate = False
    return logger


def get_root_logger(level: str = "DEBUG") -> logging.Logger:
    """Return the root vmigrate logger (not VM-specific).

    Used for orchestrator-level messages that are not tied to a single VM.

    Args:
        level: Logging level string.

    Returns:
        Root ``vmigrate`` logger.
    """
    numeric_level = getattr(logging, level.upper(), logging.INFO)
    logger = logging.getLogger("vmigrate")
    logger.setLevel(numeric_level)

    if not logger.handlers:
        rich_handler = RichHandler(
            console=_CONSOLE,
            rich_tracebacks=True,
            show_path=False,
            markup=True,
        )
        rich_handler.setLevel(numeric_level)
        logger.addHandler(rich_handler)
        logger.propagate = False

    return logger


def phase_log(
    logger: logging.Logger,
    phase: str,
    vm: str,
    status: str,
    **kwargs: object,
) -> None:
    """Emit a structured log line for a phase transition.

    The line includes ``phase``, ``vm``, ``status``, plus any additional
    keyword arguments as ``key=value`` pairs.  This format is easy to grep
    and parse by log aggregators.

    Example output::

        [PHASE] EXPORT_DISK | vm=web-01 status=RUNNING size_gb=50

    Args:
        logger: The logger to write to.
        phase: Phase name string (e.g. "EXPORT_DISK").
        vm: VM name.
        status: Status string (e.g. "RUNNING", "SUCCESS", "FAILED").
        **kwargs: Additional structured fields to include in the log line.
    """
    extra_parts = " ".join(f"{k}={v}" for k, v in kwargs.items())
    message = f"[PHASE] {phase} | vm={vm} status={status}"
    if extra_parts:
        message = f"{message} {extra_parts}"

    if status == "FAILED":
        logger.error(message)
    elif status in ("SUCCESS", "COMPLETED"):
        logger.info(message)
    else:
        logger.info(message)
