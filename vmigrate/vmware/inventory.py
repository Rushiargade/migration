"""VMware vSphere inventory inspection for vmigrate.

Provides VM discovery and detailed hardware introspection using pyVmomi's
container views.
"""

from __future__ import annotations

import logging
from typing import Optional

from pyVmomi import vim  # type: ignore

from vmigrate.vmware.client import VMwareClient

logger = logging.getLogger("vmigrate.vmware.inventory")


class VMwareInventory:
    """Query VMware vCenter inventory for VMs and their hardware details.

    Example::

        inventory = VMwareInventory(client)
        vm = inventory.find_vm("web-server-01", "DC1")
        info = inventory.get_vm_info(vm)
    """

    def __init__(self, client: VMwareClient) -> None:
        """Initialise with a connected VMwareClient.

        Args:
            client: An active :class:`VMwareClient` instance.
        """
        self._client = client

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_datacenter(self, name: str) -> vim.Datacenter:
        """Return a Datacenter object by name.

        Args:
            name: Datacenter display name as shown in vCenter.

        Returns:
            ``vim.Datacenter`` object.

        Raises:
            ValueError: If the datacenter is not found.
        """
        si = self._client.get_service_instance()
        content = si.content  # type: ignore[union-attr]
        for dc in content.rootFolder.childEntity:
            if isinstance(dc, vim.Datacenter) and dc.name == name:
                return dc
        raise ValueError(
            f"Datacenter '{name}' not found in vCenter. "
            "Check the 'datacenter' setting in your config file."
        )

    def _get_container_view(
        self, datacenter: vim.Datacenter, obj_type: list
    ) -> list:
        """Return all objects of ``obj_type`` within ``datacenter``."""
        si = self._client.get_service_instance()
        content = si.content  # type: ignore[union-attr]
        view = content.viewManager.CreateContainerView(
            datacenter.vmFolder, obj_type, recursive=True
        )
        items = list(view.view)
        view.Destroy()
        return items

    # ------------------------------------------------------------------
    # VM discovery
    # ------------------------------------------------------------------

    def find_vm(self, name: str, datacenter: str) -> vim.VirtualMachine:
        """Find and return a VM by exact name within a datacenter.

        Args:
            name: Exact VM name as shown in vCenter.
            datacenter: Datacenter name to search within.

        Returns:
            The matching ``vim.VirtualMachine`` object.

        Raises:
            ValueError: If the VM is not found.
        """
        dc = self._get_datacenter(datacenter)
        vms = self._get_container_view(dc, [vim.VirtualMachine])
        for vm in vms:
            if vm.name == name:
                logger.debug("Found VM '%s' in datacenter '%s'", name, datacenter)
                return vm
        raise ValueError(
            f"VM '{name}' not found in datacenter '{datacenter}'. "
            "Check that the name exactly matches the VM name in vCenter."
        )

    def list_vms(self, datacenter: str) -> list[dict]:
        """List all VMs in a datacenter with basic information.

        Args:
            datacenter: Datacenter name.

        Returns:
            List of dicts with keys: name, guest_id, power_state, num_cpus,
            memory_mb.
        """
        dc = self._get_datacenter(datacenter)
        vms = self._get_container_view(dc, [vim.VirtualMachine])
        result = []
        for vm in vms:
            try:
                summary = vm.summary
                config = summary.config
                runtime = summary.runtime
                result.append(
                    {
                        "name": vm.name,
                        "guest_id": config.guestId or "",
                        "power_state": str(runtime.powerState),
                        "num_cpus": config.numCpu,
                        "memory_mb": config.memorySizeMB,
                    }
                )
            except Exception as exc:
                logger.warning("Could not read summary for VM '%s': %s", vm.name, exc)
        return sorted(result, key=lambda x: x["name"])

    # ------------------------------------------------------------------
    # VM hardware introspection
    # ------------------------------------------------------------------

    def get_vm_info(self, vm: vim.VirtualMachine) -> dict:
        """Return detailed hardware information for a VM.

        Introspects the VM config to build a structured dict describing all
        virtual hardware.

        Args:
            vm: A ``vim.VirtualMachine`` object.

        Returns:
            Dict with keys:
            - name (str)
            - guest_id (str)
            - num_cpus (int)
            - memory_mb (int)
            - firmware (str): "bios" or "efi"
            - disks (list[dict]): Each dict has: label, size_gb, filename,
              datastore, controller_type, bus, unit
            - nics (list[dict]): Each dict has: label, portgroup, mac
        """
        config = vm.config
        hardware = config.hardware

        firmware = "efi" if getattr(config, "firmware", "bios") == "efi" else "bios"

        disks = self._extract_disks(hardware.device)
        nics = self._extract_nics(hardware.device)

        return {
            "name": vm.name,
            "guest_id": config.guestId or "",
            "num_cpus": hardware.numCPU,
            "memory_mb": hardware.memoryMB,
            "firmware": firmware,
            "disks": disks,
            "nics": nics,
        }

    def _extract_disks(self, devices: list) -> list[dict]:
        """Extract virtual disk information from a device list."""
        disks = []
        # Build a map of controller key -> controller info
        controllers: dict[int, dict] = {}
        for dev in devices:
            if isinstance(dev, (vim.vm.device.VirtualSCSIController,
                                vim.vm.device.VirtualIDEController,
                                vim.vm.device.VirtualSATAController,
                                vim.vm.device.VirtualNVMEController)):
                ctrl_type = type(dev).__name__.replace("Virtual", "")
                controllers[dev.key] = {
                    "type": ctrl_type,
                    "bus": dev.busNumber,
                }

        for dev in devices:
            if not isinstance(dev, vim.vm.device.VirtualDisk):
                continue
            backing = dev.backing
            filename = ""
            datastore_name = ""
            if hasattr(backing, "fileName"):
                filename = backing.fileName or ""
            if hasattr(backing, "datastore") and backing.datastore is not None:
                datastore_name = backing.datastore.name or ""

            ctrl_info = controllers.get(dev.controllerKey, {})
            size_gb = round((dev.capacityInKB or 0) / 1024 / 1024, 2)

            disks.append(
                {
                    "label": dev.deviceInfo.label if dev.deviceInfo else str(dev.key),
                    "size_gb": size_gb,
                    "filename": filename,
                    "datastore": datastore_name,
                    "controller_type": ctrl_info.get("type", "SCSI"),
                    "bus": ctrl_info.get("bus", 0),
                    "unit": dev.unitNumber or 0,
                    "key": dev.key,
                }
            )
        return disks

    def _extract_nics(self, devices: list) -> list[dict]:
        """Extract NIC information from a device list."""
        nics = []
        for dev in devices:
            if not isinstance(dev, vim.vm.device.VirtualEthernetCard):
                continue
            portgroup = ""
            backing = dev.backing
            if isinstance(backing, vim.vm.device.VirtualEthernetCard.NetworkBackingInfo):
                portgroup = backing.deviceName or ""
            elif isinstance(backing, vim.vm.device.VirtualEthernetCard.DistributedVirtualPortBackingInfo):
                portgroup = (
                    backing.port.portgroupKey or ""
                    if backing.port else ""
                )
            nics.append(
                {
                    "label": dev.deviceInfo.label if dev.deviceInfo else str(dev.key),
                    "portgroup": portgroup,
                    "mac": dev.macAddress or "",
                }
            )
        return nics
