"""Windows VM conversion using virt-v2v for vmigrate.

virt-v2v handles Windows guest conversion including VirtIO driver injection,
which is required for the converted VM to boot correctly on Proxmox/KVM.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Optional

from vmigrate.utils.ssh import SSHClient

logger = logging.getLogger("vmigrate.conversion.virt_v2v")

_PROGRESS_RE = re.compile(r"(\d+)%")


class VirtV2VConverter:
    """Convert Windows VMDKs to qcow2 with VirtIO driver injection.

    Requires ``virt-v2v`` to be installed on the conversion host
    (typically a RHEL/Fedora/CentOS machine).  ``virt-v2v`` handles:

    - Converting the VMDK to qcow2/raw
    - Injecting VirtIO storage and network drivers
    - Removing VMware Tools
    - Configuring the Windows boot loader for KVM

    Example::

        converter = VirtV2VConverter(
            ssh=ssh_client,
            virtio_iso_path="/opt/virtio-win/virtio-win.iso",
        )
        qcow2 = converter.convert(
            vmdk_path=Path("/tmp/winvm.vmdk"),
            output_dir=Path("/tmp/winvm-out"),
            vm_name="winvm",
            network_bridge="vmbr0",
        )
    """

    def __init__(
        self,
        ssh: SSHClient,
        virtio_iso_path: Optional[str] = None,
    ) -> None:
        """Initialise the converter.

        Args:
            ssh: Connected :class:`SSHClient` pointing at the conversion host.
                ``virt-v2v`` must be installed there.
            virtio_iso_path: Path on the conversion host to the VirtIO driver
                ISO.  Download from
                https://fedorapeople.org/groups/virt/virtio-win/.
                If ``None``, virt-v2v will use whatever drivers it finds on
                the system.
        """
        self._ssh = ssh
        self._virtio_iso_path = virtio_iso_path

    # ------------------------------------------------------------------
    # Conversion
    # ------------------------------------------------------------------

    def convert(
        self,
        vmdk_path: Path,
        output_dir: Path,
        vm_name: str,
        network_bridge: str,
    ) -> Path:
        """Convert a Windows VMDK using virt-v2v.

        Runs::

            virt-v2v -i disk <vmdk> -o local -os <output_dir> -of qcow2
                     --bridge <bridge> [--virtio-win-dir <iso>]

        Progress is streamed from stderr and logged.

        Args:
            vmdk_path: Path to the source VMDK on the conversion host.
            output_dir: Output directory on the conversion host.
            vm_name: VM name used for the output file naming.
            network_bridge: Proxmox bridge name (e.g. "vmbr0") that
                virt-v2v will configure inside the guest's network adapter.

        Returns:
            Path to the output qcow2 disk image on the conversion host.

        Raises:
            RuntimeError: If virt-v2v fails or is not found.
        """
        # Ensure output directory exists on the conversion host
        self._ssh.run(f"mkdir -p {output_dir}", timeout=10)

        # Force-clear Windows Hibernation / Fast Restart dirty bits on NTFS partitions first.
        # This prevents virt-v2v's driver injection from failing with the infamous
        # "filesystem was mounted read-only" ntfs-3g error.
        ntfs_fix_script = f"""
PARTS=$(guestfish --ro -a {vmdk_path} run : list-filesystems | awk -F: '/ntfs/ {{print $1}}')
for PART in $PARTS; do
    echo "Automatically clearing NTFS dirty/hibernation bit on $PART..."
    guestfish --rw -a {vmdk_path} run : ntfsfix $PART || true
done
"""
        logger.info("Executing pre-flight NTFS hibernation cleanup for %s", vm_name)
        self._ssh.run(ntfs_fix_script, timeout=300)

        cmd_parts = [
            "virt-v2v",
            "-i", "disk", str(vmdk_path),
            "-o", "local",
            "-os", str(output_dir),
            "-of", "qcow2",
            "--bridge", network_bridge,
        ]

        cmd = " ".join(cmd_parts)
        if self._virtio_iso_path:
            # virt-v2v ignores --virtio-win-dir, it expects the VIRTIO_WIN env var
            cmd = f"VIRTIO_WIN={self._virtio_iso_path} {cmd}"
        logger.info(
            "Starting virt-v2v conversion: VM='%s' src=%s out=%s",
            vm_name,
            vmdk_path.name,
            output_dir,
        )
        logger.debug("virt-v2v command: %s", cmd)

        rc, stdout, stderr = self._ssh.run(cmd, timeout=14400)  # 4 hours

        if rc != 0:
            raise RuntimeError(
                f"virt-v2v failed (exit={rc}) for VM '{vm_name}':\n"
                f"  command: {cmd}\n"
                f"  stderr (last 1000 chars): {stderr[-1000:].strip()}\n"
                "Common causes:\n"
                "  - Missing VirtIO ISO: download from "
                "https://fedorapeople.org/groups/virt/virtio-win/\n"
                "  - Unsupported Windows version\n"
                "  - VMDK file is locked or corrupted"
            )

        # virt-v2v outputs files named like: vm_name-sda (qcow2)
        # Try to find the output file
        output_path = self._find_output_file(output_dir, vm_name)
        logger.info("virt-v2v conversion complete: %s", output_path)
        return output_path

    def _find_output_file(self, output_dir: Path, vm_name: str) -> Path:
        """Locate the qcow2 output file produced by virt-v2v.

        virt-v2v names output files as ``{vm_name}-sda``, ``{vm_name}-sdb``,
        etc. for the primary disk we want ``-sda``.

        Args:
            output_dir: Directory where virt-v2v wrote its output.
            vm_name: VM name prefix.

        Returns:
            Path to the primary qcow2 output file.

        Raises:
            RuntimeError: If no output file can be found.
        """
        # List files in the output directory on the remote host
        rc, stdout, _ = self._ssh.run(
            f"ls {output_dir}/{vm_name}* 2>/dev/null || true",
            timeout=10,
        )
        files = [f.strip() for f in stdout.splitlines() if f.strip()]

        # Prefer *-sda (primary disk)
        for f in sorted(files):
            if f.endswith("-sda") or f.endswith("-sda.qcow2"):
                return Path(f)

        # Fall back to any qcow2 file in the directory
        rc2, stdout2, _ = self._ssh.run(
            f"ls {output_dir}/*.qcow2 2>/dev/null || true",
            timeout=10,
        )
        qcow2_files = [f.strip() for f in stdout2.splitlines() if f.strip()]
        if qcow2_files:
            return Path(sorted(qcow2_files)[0])

        raise RuntimeError(
            f"Could not find virt-v2v output file in {output_dir} for VM "
            f"'{vm_name}'. Expected a file matching '{vm_name}-sda' or "
            "'*.qcow2'. Check the virt-v2v logs for errors."
        )

    # ------------------------------------------------------------------
    # Availability check
    # ------------------------------------------------------------------

    def is_available(self) -> bool:
        """Check whether ``virt-v2v`` is available on the conversion host.

        Returns:
            ``True`` if ``virt-v2v`` can be found and executed.
        """
        rc, stdout, _ = self._ssh.run("which virt-v2v", timeout=10)
        if rc == 0:
            logger.debug("virt-v2v found at: %s", stdout.strip())
            return True
        logger.warning(
            "virt-v2v not found on conversion host. "
            "Install with: dnf install virt-v2v (RHEL/Fedora) or "
            "apt install virt-v2v (Debian/Ubuntu)."
        )
        return False
