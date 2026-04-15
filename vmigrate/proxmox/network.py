"""Proxmox NIC configuration for vmigrate.

Adds virtual network interfaces to migrated VMs, mapping VMware portgroups
to Proxmox Linux bridges with appropriate NIC models for the guest OS.
"""

from __future__ import annotations

import logging
from typing import Optional

from vmigrate.proxmox.client import ProxmoxClient

logger = logging.getLogger("vmigrate.proxmox.network")

# VMware guest ID prefixes that indicate Windows guests
_WINDOWS_GUEST_PREFIXES = ("windows", "win")


class NetworkManager:
    """Configure network interfaces on Proxmox VMs.

    Maps VMware NIC portgroups to Proxmox bridges and selects appropriate
    NIC models (``virtio`` for Linux, ``e1000`` for Windows before driver
    injection).

    Example::

        net_mgr = NetworkManager(client)
        net_mgr.add_nic(200, "pve1", 0, bridge="vmbr0", model="virtio",
                        mac="00:50:56:ab:cd:ef", vlan=100)
    """

    def __init__(self, client: ProxmoxClient) -> None:
        """Initialise with a connected ProxmoxClient.

        Args:
            client: An active :class:`ProxmoxClient` instance.
        """
        self._client = client

    # ------------------------------------------------------------------
    # NIC model mapping
    # ------------------------------------------------------------------

    def _map_vm_nic_model(self, guest_id: str) -> str:
        """Return the appropriate Proxmox NIC model for a guest OS.

        Windows guests use ``e1000`` because VirtIO network drivers are
        typically not pre-installed.  Linux guests use ``virtio`` for best
        performance.

        Args:
            guest_id: VMware guest OS identifier string
                (e.g. "windows9_64Guest", "rhel8_64Guest").

        Returns:
            Proxmox NIC model string: "e1000" or "virtio".
        """
        guest_lower = guest_id.lower()
        if any(guest_lower.startswith(p) for p in _WINDOWS_GUEST_PREFIXES):
            return "e1000"
        return "virtio"

    # ------------------------------------------------------------------
    # NIC addition
    # ------------------------------------------------------------------

    def add_nic(
        self,
        vmid: int,
        node: str,
        index: int,
        bridge: str,
        model: str,
        mac: Optional[str] = None,
        vlan: Optional[int] = None,
    ) -> None:
        """Add a network interface to a Proxmox VM.

        Args:
            vmid: Proxmox VMID.
            node: Proxmox node name.
            index: NIC index (0 = net0, 1 = net1, etc.).
            bridge: Proxmox Linux bridge name (e.g. "vmbr0").
            model: NIC model: "virtio", "e1000", "vmxnet3", etc.
            mac: Optional MAC address to preserve from the VMware guest.
                If ``None``, Proxmox will auto-assign one.
            vlan: Optional VLAN tag for the bridge port.

        Raises:
            RuntimeError: If the API call fails.
        """
        api = self._client.get_api()
        nic_key = f"net{index}"

        # Build the NIC parameter string
        # Format: model=bridge[,tag=VLAN][,macaddr=MAC][,firewall=0]
        nic_parts = [f"{model}={bridge}"]
        if vlan is not None:
            nic_parts.append(f"tag={vlan}")
        if mac:
            # Normalise MAC format (ensure colons, uppercase)
            normalised_mac = mac.upper().replace("-", ":")
            nic_parts.append(f"macaddr={normalised_mac}")
        nic_parts.append("firewall=0")

        nic_value = ",".join(nic_parts)

        logger.info(
            "Adding NIC net%d to vmid=%d: model=%s bridge=%s vlan=%s mac=%s",
            index,
            vmid,
            model,
            bridge,
            vlan,
            mac,
        )

        try:
            api.nodes(node).qemu(vmid).config.put(  # type: ignore[union-attr]
                **{nic_key: nic_value}
            )
        except Exception as exc:
            raise RuntimeError(
                f"Failed to add NIC {nic_key} to vmid={vmid}: {exc}\n"
                f"  bridge='{bridge}' model='{model}'\n"
                "Ensure the bridge exists on the Proxmox node."
            ) from exc

        logger.info("NIC net%d added to vmid=%d", index, vmid)
