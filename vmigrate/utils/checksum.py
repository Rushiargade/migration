"""Checksum and disk image verification utilities for vmigrate.

Provides SHA-256 file hashing and qcow2 image integrity verification using
``qemu-img check``.
"""

from __future__ import annotations

import hashlib
import logging
import subprocess
from pathlib import Path

logger = logging.getLogger("vmigrate.checksum")

_CHUNK_SIZE = 1024 * 1024  # 1 MiB read chunks


def sha256_file(path: Path) -> str:
    """Compute and return the SHA-256 hex digest of a file.

    Reads the file in chunks so that large disk images do not exhaust memory.

    Args:
        path: Path to the file to hash.

    Returns:
        Lowercase hexadecimal SHA-256 digest string.

    Raises:
        FileNotFoundError: If ``path`` does not exist.
        OSError: On read errors.
    """
    if not path.exists():
        raise FileNotFoundError(
            f"Cannot compute checksum: file not found: {path}"
        )

    h = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(_CHUNK_SIZE):
            h.update(chunk)
    digest = h.hexdigest()
    logger.debug("sha256(%s) = %s", path.name, digest)
    return digest


def verify_qcow2(path: Path) -> bool:
    """Verify a qcow2 image file using ``qemu-img check``.

    Runs ``qemu-img check <path>`` and returns ``True`` if the image passes
    all integrity checks (exit code 0).  A non-zero exit code means the image
    is corrupt or has leaked clusters.

    Args:
        path: Path to the qcow2 image file.

    Returns:
        ``True`` if the image is healthy, ``False`` otherwise.

    Raises:
        FileNotFoundError: If the image file does not exist.
        OSError: If ``qemu-img`` is not installed or cannot be executed.
            Install it with: ``apt install qemu-utils`` or
            ``dnf install qemu-img``.
    """
    if not path.exists():
        raise FileNotFoundError(
            f"Cannot verify qcow2: file not found: {path}"
        )

    cmd = ["qemu-img", "check", str(path)]
    logger.debug("Running: %s", " ".join(cmd))
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=300,
        )
    except FileNotFoundError:
        raise OSError(
            "qemu-img not found. Install it with: apt install qemu-utils "
            "(Debian/Ubuntu) or dnf install qemu-img (RHEL/Fedora)."
        )
    except subprocess.TimeoutExpired:
        logger.error("qemu-img check timed out for %s", path)
        return False

    if result.returncode == 0:
        logger.debug("qemu-img check PASSED for %s", path.name)
        return True
    else:
        logger.warning(
            "qemu-img check FAILED for %s (exit=%d):\nstdout: %s\nstderr: %s",
            path.name,
            result.returncode,
            result.stdout.strip(),
            result.stderr.strip(),
        )
        return False
