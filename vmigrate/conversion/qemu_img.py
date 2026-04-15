"""VMDK-to-qcow2 conversion using qemu-img for Linux VMs.

Runs ``qemu-img convert`` either locally or via SSH to a conversion host.
Progress is parsed from the ``-p`` flag output and logged.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
from pathlib import Path
from typing import Optional

from vmigrate.utils.ssh import SSHClient

logger = logging.getLogger("vmigrate.conversion.qemu_img")

_PROGRESS_RE = re.compile(r"\((\d+\.\d+)/100%\)")


class QemuImgConverter:
    """Convert VMDK images to qcow2 format using ``qemu-img``.

    If an :class:`SSHClient` is provided, commands are executed on the remote
    conversion host; otherwise they run locally.  The conversion host must
    have ``qemu-img`` installed.

    Example::

        converter = QemuImgConverter(ssh=ssh_client)
        qcow2_path = converter.convert(
            vmdk_path=Path("/tmp/disk.vmdk"),
            output_path=Path("/tmp/disk.qcow2"),
        )
    """

    def __init__(self, ssh: Optional[SSHClient] = None) -> None:
        """Initialise the converter.

        Args:
            ssh: Optional connected :class:`SSHClient`.  If ``None``,
                ``qemu-img`` is executed as a local subprocess.
        """
        self._ssh = ssh

    # ------------------------------------------------------------------
    # Conversion
    # ------------------------------------------------------------------

    def convert(
        self,
        vmdk_path: Path,
        output_path: Path,
        sparse: bool = True,
    ) -> Path:
        """Convert a VMDK to qcow2 format.

        Runs::

            qemu-img convert -f vmdk -O qcow2 -p -W [-S 4k] input output

        The ``-p`` flag prints progress to stdout which is captured and
        logged.  ``-W`` enables parallel writes for better performance.
        ``-S 4k`` enables sparse output (skip zero blocks) when
        ``sparse=True``.

        Args:
            vmdk_path: Source VMDK file path.
            output_path: Destination qcow2 file path.
            sparse: Enable sparse output (skip zero sectors).  Saves disk
                space when the source disk has large empty regions.

        Returns:
            Path to the written qcow2 file (same as ``output_path``).

        Raises:
            RuntimeError: If conversion fails or ``qemu-img`` is not found.
        """
        # Always use forward slashes for paths — when running on Windows and
        # executing remotely via SSH the backslashes from Path.str() break Linux.
        vmdk_str   = vmdk_path.as_posix()   if hasattr(vmdk_path,   'as_posix') else str(vmdk_path).replace("\\", "/")
        output_str = output_path.as_posix() if hasattr(output_path, 'as_posix') else str(output_path).replace("\\", "/")

        cmd_parts = [
            "qemu-img",
            "convert",
            "-f", "vmdk",
            "-O", "qcow2",
            "-p",
            "-W",
        ]
        if sparse:
            cmd_parts += ["-S", "4k"]
        cmd_parts += [vmdk_str, output_str]
        cmd = " ".join(cmd_parts)

        logger.info(
            "Converting VMDK -> qcow2: %s -> %s (sparse=%s)",
            vmdk_path.name if hasattr(vmdk_path, 'name') else vmdk_str,
            output_path.name if hasattr(output_path, 'name') else output_str,
            sparse,
        )

        if self._ssh is not None:
            # Ensure output directory exists on the remote host
            parent = output_path.parent.as_posix() if hasattr(output_path, 'as_posix') else str(output_path.parent).replace("\\", "/")
            self._ssh.run(f"mkdir -p {parent}")
            rc, stdout, stderr = self._ssh.run(cmd, timeout=7200)
        else:
            result = subprocess.run(
                cmd_parts,
                capture_output=True,
                text=True,
                timeout=7200,
            )
            rc = result.returncode
            stdout = result.stdout
            stderr = result.stderr

        if rc != 0:
            raise RuntimeError(
                f"qemu-img convert failed (exit={rc}):\n"
                f"  command: {cmd}\n"
                f"  stderr: {stderr.strip()}\n"
                "Ensure qemu-utils is installed and the VMDK file is not "
                "corrupted."
            )

        logger.info(
            "Conversion complete: %s",
            output_path.name,
        )
        return output_path

    # ------------------------------------------------------------------
    # Integrity check
    # ------------------------------------------------------------------

    def check(self, qcow2_path: Path) -> bool:
        """Run ``qemu-img check`` on a qcow2 image.

        Args:
            qcow2_path: Path to the qcow2 image.

        Returns:
            ``True`` if the image passes integrity checks, ``False``
            otherwise.
        """
        path_str = qcow2_path.as_posix() if hasattr(qcow2_path, 'as_posix') else str(qcow2_path).replace("\\", "/")
        cmd = f"qemu-img check {path_str}"
        logger.debug("Running qemu-img check on %s", path_str)

        if self._ssh is not None:
            rc, stdout, stderr = self._ssh.run(cmd, timeout=300)
        else:
            result = subprocess.run(
                ["qemu-img", "check", str(qcow2_path)],
                capture_output=True,
                text=True,
                timeout=300,
            )
            rc = result.returncode
            stdout = result.stdout
            stderr = result.stderr

        if rc == 0:
            logger.debug("qemu-img check PASSED for %s", qcow2_path.name)
            return True

        logger.warning(
            "qemu-img check FAILED for %s (exit=%d): %s",
            qcow2_path.name,
            rc,
            (stderr or stdout).strip(),
        )
        return False

    # ------------------------------------------------------------------
    # Image info
    # ------------------------------------------------------------------

    def info(self, path: Path) -> dict:
        """Return metadata about a disk image as a dict.

        Runs ``qemu-img info --output=json`` and parses the result.

        Args:
            path: Path to the disk image.

        Returns:
            Dict with image metadata (virtual_size, actual_size, format, etc.).

        Raises:
            RuntimeError: If ``qemu-img info`` fails.
        """
        path_str = path.as_posix() if hasattr(path, 'as_posix') else str(path).replace("\\", "/")
        cmd = f"qemu-img info --output=json {path_str}"

        if self._ssh is not None:
            rc, stdout, stderr = self._ssh.run(cmd, timeout=60)
        else:
            result = subprocess.run(
                ["qemu-img", "info", "--output=json", str(path)],
                capture_output=True,
                text=True,
                timeout=60,
            )
            rc = result.returncode
            stdout = result.stdout
            stderr = result.stderr

        if rc != 0:
            raise RuntimeError(
                f"qemu-img info failed (exit={rc}): {stderr.strip()}"
            )

        try:
            return json.loads(stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                f"Failed to parse qemu-img info output as JSON: {exc}\n"
                f"Output was: {stdout[:500]}"
            ) from exc

    # ------------------------------------------------------------------
    # Availability check
    # ------------------------------------------------------------------

    def is_available(self) -> bool:
        """Check whether ``qemu-img`` is available on the target host.

        Returns:
            ``True`` if ``qemu-img`` can be found and executed.
        """
        cmd = "qemu-img --version"
        if self._ssh is not None:
            rc, _, _ = self._ssh.run(cmd, timeout=10)
        else:
            result = subprocess.run(
                ["qemu-img", "--version"],
                capture_output=True,
                timeout=10,
            )
            rc = result.returncode
        return rc == 0
