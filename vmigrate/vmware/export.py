"""VMware disk export utilities for vmigrate.

Exports VMDK files from vSphere using the NFC (Network File Copy) lease
protocol, with an HTTP fallback for datastores accessible via HTTPS.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import requests

from pyVmomi import vim  # type: ignore

from vmigrate.vmware.client import VMwareClient

logger = logging.getLogger("vmigrate.vmware.export")

_CHUNK_SIZE = 4 * 1024 * 1024  # 4 MiB


class VMwareExporter:
    """Export VM disk images from vSphere to local storage.

    Supports two export methods:
    1. NFC lease (preferred): streams VMDK directly from ESXi using the
       ``ExportVm`` HttpNfcLease mechanism.
    2. HTTP fallback: downloads VMDK from the ESXi HTTPS file server.

    Example::

        exporter = VMwareExporter(client)
        exported = exporter.export_vm_disks(vm, output_dir=Path("/tmp/vm01"))
    """

    def __init__(self, client: VMwareClient) -> None:
        """Initialise with a connected VMwareClient.

        Args:
            client: An active :class:`VMwareClient` instance.
        """
        self._client = client

    # ------------------------------------------------------------------
    # NFC export
    # ------------------------------------------------------------------

    def export_disk_nfc(
        self,
        vm: vim.VirtualMachine,
        disk_label: str,
        output_path: Path,
        snapshot_moref: Optional[str] = None,
        progress_file: Optional[Path] = None,
        disk_index: int = 1,
        disk_count: int = 1,
    ) -> Path:
        """Export a single VMDK via the NFC lease mechanism.

        Obtains an ``HttpNfcLease`` from vSphere, streams the flat VMDK file
        to ``output_path``, and updates the lease progress as transfer
        proceeds.

        Args:
            vm: Source ``vim.VirtualMachine``.
            disk_label: Label of the virtual disk to export (e.g. "Hard disk 1").
            output_path: Local file path to write the VMDK to.
            snapshot_moref: If set, export the disk as it appeared at this
                snapshot rather than the current live disk.

        Returns:
            Path to the downloaded VMDK file.

        Raises:
            RuntimeError: If the lease cannot be obtained or the download fails.
        """
        output_path.parent.mkdir(parents=True, exist_ok=True)

        logger.info(
            "Starting NFC export: VM='%s' disk='%s' -> %s",
            vm.name,
            disk_label,
            output_path,
        )

        lease = vm.ExportVm()
        self._wait_for_lease(lease)

        try:
            # Log all device URLs so we can see exactly what ESXi provides
            logger.info("NFC lease ready. Device URLs for VM '%s':", vm.name)
            for du in lease.info.deviceUrl:
                logger.info(
                    "  key=%-40s  targetId=%-20s  url=%s",
                    du.key, du.targetId, du.url,
                )

            # vCenter NFC URLs use opaque names like disk-0.vmdk, disk-1.vmdk …
            # They do NOT embed the human-readable disk label, so we cannot
            # match by label.  Instead we:
            #   1. Collect all URLs whose path ends in .vmdk (excludes .nvram etc.)
            #   2. Sort them so disk-0 < disk-1 < disk-2 (consistent ordering)
            #   3. Pick by disk_index (1-based caller param → 0-based list index)
            # This works because vCenter assigns disk-N in the same order as the
            # SCSI controller slot numbers, matching the enumeration order in
            # vm.config.hardware.device used by export_vm_disks.
            all_du = list(lease.info.deviceUrl)  # materialise — pyVmomi array

            vmdk_du = sorted(
                [du for du in all_du if du.url and du.url.lower().endswith(".vmdk")],
                key=lambda du: du.url.lower(),
            )

            url: str | None = None
            if vmdk_du:
                idx = disk_index - 1  # convert 1-based → 0-based
                url = vmdk_du[idx].url if idx < len(vmdk_du) else vmdk_du[-1].url
            elif all_du:
                # No .vmdk URLs at all — fall back to first entry and log a warning
                logger.warning(
                    "No .vmdk URLs found in NFC lease for VM '%s'. "
                    "Falling back to first device URL.",
                    vm.name,
                )
                url = all_du[0].url

            if url is None:
                raise RuntimeError(
                    f"Could not find a download URL for disk '{disk_label}' "
                    f"on VM '{vm.name}'. Available devices: "
                    f"{[d.targetId for d in all_du]}"
                )

            if url is None:
                raise RuntimeError(
                    f"Could not find a download URL for disk '{disk_label}' "
                    f"on VM '{vm.name}'. Available devices: "
                    f"{[d.targetId for d in lease.info.deviceUrl]}"
                )

            # Replace * with the ESXi host in the URL (common in NFC URLs)
            if url.startswith("https://*"):
                url = url.replace("*", self._client._config.host, 1)

            logger.info("Selected URL for disk '%s': %s", disk_label, url)

            total_bytes = self._stream_download(
                url, output_path, lease,
                progress_file=progress_file,
                disk_label=disk_label,
                disk_index=disk_index,
                disk_count=disk_count,
            )
            return output_path

        finally:
            try:
                lease.HttpNfcLeaseComplete()
            except Exception as exc:
                logger.warning("Error completing NFC lease: %s", exc)

    def _wait_for_lease(self, lease: object, timeout: int = 120) -> None:
        """Wait for an HttpNfcLease to reach the 'ready' state."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            state = lease.state  # type: ignore[union-attr]
            if state == vim.HttpNfcLease.State.ready:
                return
            if state == vim.HttpNfcLease.State.error:
                raise RuntimeError(
                    f"HttpNfcLease entered error state: {lease.error}"  # type: ignore[union-attr]
                )
            time.sleep(1)
        raise RuntimeError(
            f"HttpNfcLease did not become ready within {timeout} seconds."
        )

    def _stream_download(
        self,
        url: str,
        output_path: Path,
        lease: object,
        verify_ssl: bool = False,
        progress_file: Optional[Path] = None,
        disk_label: str = "",
        disk_index: int = 1,
        disk_count: int = 1,
    ) -> int:
        """Stream an HTTPS download to a local file, updating lease progress.

        Args:
            url: HTTPS URL to download.
            output_path: Local destination path.
            lease: Active ``vim.HttpNfcLease`` for progress reporting.
            verify_ssl: Whether to verify the server SSL certificate.

        Returns:
            Total bytes downloaded.
        """
        session = requests.Session()
        session.verify = verify_ssl

        # Add vSphere cookie from the current session
        si = self._client.get_service_instance()
        cookie = si._stub.cookie  # type: ignore[union-attr]
        if cookie:
            session.headers["Cookie"] = cookie

        response = session.get(url, stream=True, timeout=120)
        logger.info(
            "HTTP %d from ESXi — Content-Length: %s  Content-Type: %s",
            response.status_code,
            response.headers.get("Content-Length", "none"),
            response.headers.get("Content-Type", "unknown"),
        )
        response.raise_for_status()

        total_size = int(response.headers.get("Content-Length", 0))
        total_mb = total_size / 1024 / 1024
        size_known = total_size > 0
        downloaded = 0
        last_pct = -1
        last_progress_write = 0.0   # monotonic time of last progress-file write

        logger.info(
            "Downloading %s — total size: %s  (disk %d/%d)",
            output_path.name,
            f"{total_mb:.1f} MB" if size_known else "unknown (no Content-Length)",
            disk_index,
            disk_count,
        )

        start_time = time.monotonic()
        _stop_keepalive = threading.Event()

        def _write_progress(pct: int, speed_mbps: float, eta_s: float) -> None:
            if progress_file is None:
                return
            try:
                progress_file.parent.mkdir(parents=True, exist_ok=True)
                progress_file.write_text(json.dumps({
                    "disk_label":    disk_label,
                    "disk_index":    disk_index,
                    "disk_count":    disk_count,
                    "pct":           pct,
                    "downloaded_mb": round(downloaded / 1024 / 1024, 1),
                    "total_mb":      round(total_mb, 1),
                    "speed_mbps":    round(speed_mbps, 1),
                    "eta_s":         int(eta_s),
                    "size_known":    size_known,
                    "phase":         "downloading",
                }))
            except Exception:
                pass

        # NFC lease keepalive — ESXi drops the lease after ~5 min with no update
        def _keepalive():
            while not _stop_keepalive.wait(30):
                try:
                    pct = int(downloaded / total_size * 100) if size_known else 0
                    lease.HttpNfcLeaseProgress(pct)  # type: ignore[union-attr]
                except Exception:
                    pass

        keepalive_thread = threading.Thread(target=_keepalive, daemon=True)
        keepalive_thread.start()

        try:
            with output_path.open("wb") as fh:
                for chunk in response.iter_content(chunk_size=_CHUNK_SIZE):
                    if chunk:
                        fh.write(chunk)
                        downloaded += len(chunk)
                        now = time.monotonic()
                        elapsed = now - start_time or 0.001
                        speed_mbps = (downloaded / 1024 / 1024) / elapsed

                        if size_known:
                            pct = int(downloaded / total_size * 100)
                            remaining = total_size - downloaded
                            eta_s = (remaining / 1024 / 1024) / speed_mbps if speed_mbps > 0 else 0

                            if pct >= last_pct + 5:   # log + update every 5%
                                last_pct = pct
                                logger.info(
                                    "  %s: %d%%  %.0f/%.0f MB  %.1f MB/s  ETA %ds",
                                    output_path.name, pct,
                                    downloaded / 1024 / 1024, total_mb,
                                    speed_mbps, int(eta_s),
                                )
                                _write_progress(pct, speed_mbps, eta_s)
                                last_progress_write = now
                        else:
                            # No Content-Length — write progress every 2 s so UI stays live
                            if now - last_progress_write >= 2.0:
                                dl_mb = downloaded / 1024 / 1024
                                logger.info(
                                    "  %s: %.0f MB downloaded  %.1f MB/s  (size unknown)",
                                    output_path.name, dl_mb, speed_mbps,
                                )
                                _write_progress(0, speed_mbps, 0)
                                last_progress_write = now
        finally:
            _stop_keepalive.set()
            # Mark as done in progress file
            _write_progress(100, 0, 0)
            if progress_file:
                try:
                    data = json.loads(progress_file.read_text())
                    data["phase"] = "done"
                    progress_file.write_text(json.dumps(data))
                except Exception:
                    pass

        logger.info(
            "Download complete: %s — %.1f MB in %.0fs",
            output_path.name, downloaded / 1024 / 1024,
            time.monotonic() - start_time,
        )
        return downloaded

    # ------------------------------------------------------------------
    # HTTP fallback export
    # ------------------------------------------------------------------

    def export_disk_http(
        self,
        vm: vim.VirtualMachine,
        disk_filename: str,
        datastore_url: str,
        output_path: Path,
    ) -> Path:
        """Download a VMDK from the ESXi HTTPS file server (fallback method).

        Constructs the ESXi datastore browser URL and downloads the flat VMDK.
        This method works without an NFC lease but may be slower.

        Args:
            vm: Source VM (used for session cookie).
            disk_filename: VMDK filename as stored in the datastore
                (e.g. "[datastore1] vm/vm-flat.vmdk").
            datastore_url: HTTPS base URL of the ESXi datastore browser.
            output_path: Local file path to write the downloaded VMDK to.

        Returns:
            Path to the downloaded VMDK file.
        """
        output_path.parent.mkdir(parents=True, exist_ok=True)

        # Strip bracket notation from datastore filename if present
        # "[datastore1] folder/file.vmdk" -> "folder/file.vmdk"
        clean_path = disk_filename
        if "]" in disk_filename:
            clean_path = disk_filename.split("]", 1)[1].strip()

        url = f"{datastore_url.rstrip('/')}/{clean_path}"
        logger.info("HTTP export fallback: downloading %s", url)

        si = self._client.get_service_instance()
        cookie = si._stub.cookie  # type: ignore[union-attr]

        session = requests.Session()
        session.verify = self._client._config.verify_ssl
        if cookie:
            session.headers["Cookie"] = cookie

        response = session.get(url, stream=True, timeout=60)
        response.raise_for_status()

        total = 0
        with output_path.open("wb") as fh:
            for chunk in response.iter_content(chunk_size=_CHUNK_SIZE):
                if chunk:
                    fh.write(chunk)
                    total += len(chunk)

        logger.info(
            "HTTP export complete: %s (%.1f MB)",
            output_path.name,
            total / 1024 / 1024,
        )
        return output_path

    # ------------------------------------------------------------------
    # Bulk disk export
    # ------------------------------------------------------------------

    def export_vm_disks(
        self,
        vm: vim.VirtualMachine,
        output_dir: Path,
        snapshot_moref: Optional[str] = None,
    ) -> list[dict]:
        """Export all virtual disks of a VM to a local directory.

        Iterates over all virtual disk devices and exports each one using the
        NFC method.

        Args:
            vm: Source ``vim.VirtualMachine``.
            output_dir: Directory to write disk images to.  Created if it
                does not exist.
            snapshot_moref: Optional snapshot to export from.

        Returns:
            List of dicts with keys: label, local_path (Path), size_bytes.
        """
        output_dir.mkdir(parents=True, exist_ok=True)
        hardware = vm.config.hardware
        disks = [
            dev
            for dev in hardware.device
            if isinstance(dev, vim.vm.device.VirtualDisk)
        ]

        if not disks:
            logger.warning("VM '%s' has no virtual disks to export.", vm.name)
            return []

        disk_count = len(disks)
        progress_file = output_dir / "export_progress.json"

        results = []
        for disk_index, disk in enumerate(disks, start=1):
            label = disk.deviceInfo.label if disk.deviceInfo else f"disk_{disk.key}"
            # Sanitise label for use as a filename
            safe_label = label.replace(" ", "_").replace("/", "-")
            vmdk_path = output_dir / f"{vm.name}_{safe_label}.vmdk"

            logger.info("Exporting disk '%s' (%d/%d) from VM '%s'...", label, disk_index, disk_count, vm.name)
            try:
                local_path = self.export_disk_nfc(
                    vm, label, vmdk_path, snapshot_moref,
                    progress_file=progress_file,
                    disk_index=disk_index,
                    disk_count=disk_count,
                )
                size_bytes = local_path.stat().st_size
                results.append(
                    {
                        "label": label,
                        "local_path": local_path,
                        "size_bytes": size_bytes,
                        "disk_key": disk.key,
                    }
                )
                logger.info(
                    "Disk '%s' exported: %.1f GB",
                    label,
                    size_bytes / 1024 / 1024 / 1024,
                )
            except Exception as exc:
                logger.error(
                    "Failed to export disk '%s' from VM '%s': %s",
                    label,
                    vm.name,
                    exc,
                )
                raise

        return results
