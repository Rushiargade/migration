"""Proxmox guest agent installation and management for vmigrate.

Handles enabling the QEMU guest agent in Proxmox VM configuration and
triggering agent installation inside the guest via exec API (Linux) or
WinRM (Windows).
"""

from __future__ import annotations

import logging
import time
from typing import Optional

from vmigrate.proxmox.client import ProxmoxClient

logger = logging.getLogger("vmigrate.proxmox.agent")

# Install commands by Linux distro family
_LINUX_INSTALL_CMDS = {
    "debian": "apt-get install -y qemu-guest-agent && systemctl enable --now qemu-guest-agent",
    "ubuntu": "apt-get install -y qemu-guest-agent && systemctl enable --now qemu-guest-agent",
    "rhel": "dnf install -y qemu-guest-agent && systemctl enable --now qemu-guest-agent",
    "centos": "yum install -y qemu-guest-agent && systemctl enable --now qemu-guest-agent",
    "fedora": "dnf install -y qemu-guest-agent && systemctl enable --now qemu-guest-agent",
    "suse": "zypper install -y qemu-guest-agent && systemctl enable --now qemu-guest-agent",
}
_DEFAULT_LINUX_CMD = (
    "which apt-get > /dev/null 2>&1 && apt-get install -y qemu-guest-agent "
    "|| which dnf > /dev/null 2>&1 && dnf install -y qemu-guest-agent "
    "|| yum install -y qemu-guest-agent; "
    "systemctl enable qemu-guest-agent 2>/dev/null || true; "
    "systemctl start qemu-guest-agent 2>/dev/null || true"
)


class AgentInstaller:
    """Install and configure the QEMU guest agent on migrated VMs.

    The QEMU guest agent enables Proxmox to interact with the guest OS for
    shutdown, freeze, and IP address reporting.

    Example::

        installer = AgentInstaller(client)
        installer.enable_agent_config(200, "pve1")
        installer.install_linux(200, "pve1", "debian")
        installer.wait_for_agent(200, "pve1", timeout=300)
    """

    def __init__(self, client: ProxmoxClient) -> None:
        """Initialise with a connected ProxmoxClient.

        Args:
            client: An active :class:`ProxmoxClient` instance.
        """
        self._client = client

    # ------------------------------------------------------------------
    # Agent configuration
    # ------------------------------------------------------------------

    def enable_agent_config(self, vmid: int, node: str) -> None:
        """Enable the guest agent in the Proxmox VM configuration.

        Sets ``agent=1`` in the VM config so Proxmox knows to communicate
        with the agent once it is running inside the guest.

        Args:
            vmid: Proxmox VMID.
            node: Proxmox node name.

        Raises:
            RuntimeError: If the config update fails.
        """
        api = self._client.get_api()
        logger.info("Enabling QEMU guest agent config for vmid=%d", vmid)
        try:
            api.nodes(node).qemu(vmid).config.put(agent="1,fstrim_cloned_disks=1")  # type: ignore[union-attr]
        except Exception as exc:
            raise RuntimeError(
                f"Failed to enable guest agent for vmid={vmid}: {exc}"
            ) from exc

    # ------------------------------------------------------------------
    # Linux installation
    # ------------------------------------------------------------------

    def install_linux(
        self,
        vmid: int,
        node: str,
        distro_family: str,
    ) -> None:
        """Install qemu-guest-agent inside a running Linux guest.

        Uses the Proxmox guest exec API to run the package manager inside the
        VM.  The VM must be running and the guest agent must already be
        reachable (use :meth:`wait_for_agent` first).

        Args:
            vmid: Proxmox VMID.
            node: Proxmox node name.
            distro_family: Linux distribution family string
                (e.g. "debian", "rhel", "fedora").  Used to select the
                correct package manager command.

        Raises:
            RuntimeError: If the exec call fails or times out.
        """
        api = self._client.get_api()
        distro_lower = distro_family.lower()
        install_cmd = _LINUX_INSTALL_CMDS.get(distro_lower, _DEFAULT_LINUX_CMD)

        logger.info(
            "Installing qemu-guest-agent on vmid=%d (distro=%s)",
            vmid,
            distro_family,
        )
        try:
            result = api.nodes(node).qemu(vmid).agent.exec.post(  # type: ignore[union-attr]
                command=f"bash -c '{install_cmd}'"
            )
            pid = result.get("pid")
            if pid:
                self._wait_for_exec(api, node, vmid, pid, timeout=300)
        except Exception as exc:
            raise RuntimeError(
                f"Failed to install guest agent on vmid={vmid}: {exc}\n"
                "Ensure the VM is running and the guest agent is reachable. "
                "You may need to install the agent manually inside the guest."
            ) from exc

    def _wait_for_exec(
        self,
        api: object,
        node: str,
        vmid: int,
        pid: int,
        timeout: int = 300,
    ) -> dict:
        """Poll for a guest exec command to complete.

        Args:
            api: proxmoxer API object.
            node: Proxmox node name.
            vmid: VMID.
            pid: Process ID from the exec call.
            timeout: Maximum seconds to wait.

        Returns:
            Dict with exec result (exitcode, out-data, err-data).
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                status = api.nodes(node).qemu(vmid).agent("exec-status").get(pid=pid)  # type: ignore[union-attr]
                if status.get("exited"):
                    return status
            except Exception:
                pass
            time.sleep(5)
        logger.warning("Guest exec pid=%d timed out after %d seconds", pid, timeout)
        return {}

    # ------------------------------------------------------------------
    # Windows installation
    # ------------------------------------------------------------------

    def install_windows(
        self,
        vmid: int,
        node: str,
        winrm_host: str,
        winrm_user: str,
        winrm_pass: str,
    ) -> None:
        """Install qemu-guest-agent on a Windows guest via WinRM.

        Connects to the guest using WinRM and runs a PowerShell command to
        install the QEMU guest agent from the VirtIO drivers package.

        Args:
            vmid: Proxmox VMID (used for logging).
            node: Proxmox node name (used for logging).
            winrm_host: IP or hostname of the Windows guest.
            winrm_user: WinRM username.
            winrm_pass: WinRM password.

        Raises:
            RuntimeError: If the WinRM connection or command fails.
        """
        try:
            import winrm  # type: ignore
        except ImportError:
            raise RuntimeError(
                "pywinrm is not installed. "
                "Install it with: pip install pywinrm"
            )

        logger.info(
            "Installing QEMU guest agent on Windows vmid=%d via WinRM host=%s",
            vmid,
            winrm_host,
        )

        ps_command = (
            "# Install QEMU guest agent via VirtIO MSI if present\n"
            "$msi = Get-ChildItem 'E:\\' -Recurse -Filter 'qemu-ga-x86_64.msi' "
            "-ErrorAction SilentlyContinue | Select-Object -First 1\n"
            "if ($msi) {\n"
            "    Start-Process msiexec -ArgumentList '/i', $msi.FullName, '/quiet', '/norestart' -Wait\n"
            "} else {\n"
            "    Write-Error 'qemu-ga-x86_64.msi not found on E: drive (VirtIO ISO)'\n"
            "    exit 1\n"
            "}"
        )

        try:
            session = winrm.Session(
                winrm_host,
                auth=(winrm_user, winrm_pass),
                transport="ntlm",
            )
            result = session.run_ps(ps_command)
            if result.status_code != 0:
                stderr = result.std_err.decode("utf-8", errors="replace")
                raise RuntimeError(
                    f"WinRM PowerShell command failed (exit={result.status_code}) "
                    f"on vmid={vmid}:\n{stderr}\n"
                    "Ensure the VirtIO ISO is attached to the VM's CD drive and "
                    "the guest is accessible via WinRM."
                )
            logger.info("QEMU guest agent installed on Windows vmid=%d", vmid)
        except Exception as exc:
            if isinstance(exc, RuntimeError):
                raise
            raise RuntimeError(
                f"WinRM connection failed to {winrm_host} for vmid={vmid}: {exc}\n"
                "Ensure WinRM is enabled on the guest: "
                "winrm quickconfig -force"
            ) from exc

    # ------------------------------------------------------------------
    # Agent readiness check
    # ------------------------------------------------------------------

    def wait_for_agent(
        self,
        vmid: int,
        node: str,
        timeout: int = 300,
    ) -> bool:
        """Wait for the QEMU guest agent to become responsive.

        Polls the Proxmox guest agent ping endpoint until it responds or
        the timeout is reached.

        Args:
            vmid: Proxmox VMID.
            node: Proxmox node name.
            timeout: Maximum seconds to wait.

        Returns:
            ``True`` if the agent responds within the timeout, ``False``
            otherwise.
        """
        api = self._client.get_api()
        deadline = time.time() + timeout
        logger.info(
            "Waiting for guest agent on vmid=%d (timeout=%ds)...", vmid, timeout
        )
        while time.time() < deadline:
            try:
                api.nodes(node).qemu(vmid).agent.ping.post()  # type: ignore[union-attr]
                logger.info("Guest agent is responsive on vmid=%d", vmid)
                return True
            except Exception:
                time.sleep(5)

        logger.warning(
            "Guest agent on vmid=%d did not respond within %d seconds. "
            "The agent may not be installed or the VM may need a reboot.",
            vmid,
            timeout,
        )
        return False
