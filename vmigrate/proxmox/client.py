"""Proxmox VE API client wrapper for vmigrate.

Wraps proxmoxer.ProxmoxAPI with connection management, node verification,
and VMID allocation.
"""

from __future__ import annotations

import logging
from typing import Optional

import urllib3

from vmigrate.config import ProxmoxConfig

logger = logging.getLogger("vmigrate.proxmox.client")


class ProxmoxClient:
    """Manages the proxmoxer API connection to a Proxmox VE cluster.

    Example::

        with ProxmoxClient(config.proxmox) as pve:
            vmid = pve.get_next_vmid()
            pve.verify_node("pve1")
    """

    def __init__(self, config: ProxmoxConfig) -> None:
        """Initialise with Proxmox connection configuration.

        Args:
            config: Populated :class:`ProxmoxConfig` dataclass.
        """
        self._config = config
        self._api: Optional[object] = None  # proxmoxer.ProxmoxAPI

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------

    def connect(self) -> None:
        """Create and authenticate the proxmoxer API connection.

        Raises:
            Exception: On connection or authentication failure.
        """
        # Import here to keep it lazy (not needed for config-only operations)
        from proxmoxer import ProxmoxAPI  # type: ignore

        if not self._config.verify_ssl:
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

        logger.info(
            "Connecting to Proxmox %s:%d as %s",
            self._config.host,
            self._config.port,
            self._config.user,
        )

        self._api = ProxmoxAPI(
            self._config.host,
            user=self._config.user,
            password=self._config.password,
            port=self._config.port,
            verify_ssl=self._config.verify_ssl,
        )
        logger.info("Connected to Proxmox %s", self._config.host)

    def get_api(self) -> object:
        """Return the proxmoxer API object.

        Returns:
            The ``proxmoxer.ProxmoxAPI`` instance.

        Raises:
            RuntimeError: If not connected.
        """
        if self._api is None:
            raise RuntimeError(
                "ProxmoxClient is not connected. "
                "Call connect() or use as a context manager."
            )
        return self._api

    # ------------------------------------------------------------------
    # Node / cluster queries
    # ------------------------------------------------------------------

    def verify_node(self, node: str) -> bool:
        """Check that ``node`` exists and is online in the Proxmox cluster.

        Args:
            node: Proxmox node name (e.g. "pve1").

        Returns:
            ``True`` if the node is online, ``False`` otherwise.
        """
        api = self.get_api()
        try:
            nodes = api.nodes.get()  # type: ignore[union-attr]
            for n in nodes:
                if n.get("node") == node and n.get("status") == "online":
                    logger.debug("Proxmox node '%s' is online.", node)
                    return True
            logger.warning(
                "Proxmox node '%s' not found or not online. "
                "Available nodes: %s",
                node,
                [n.get("node") for n in nodes],
            )
            return False
        except Exception as exc:
            logger.error("Failed to verify Proxmox node '%s': %s", node, exc)
            return False

    def get_node_ip(self, node: str, extra_candidates: list | None = None) -> str:
        """Return the IP address of a Proxmox node.

        Tries multiple methods to find a real IPv4/IPv6 address for the node.
        Falls back to the configured API host if nothing works.

        Args:
            node: Proxmox node name (e.g. "map1hr09s03").

        Returns:
            IP address string (e.g. "10.5.5.92").
        """
        import re, os as _os, socket as _sock
        _ip_re = re.compile(r"^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$")

        def _is_ip(val: str) -> bool:
            return bool(val and _ip_re.match(val.strip()))

        def _reachable(ip: str, port: int = 22, timeout: float = 3.0) -> bool:
            """Return True if ip:port accepts a TCP connection within timeout."""
            try:
                with _sock.create_connection((ip, port), timeout=timeout):
                    return True
            except OSError:
                return False

        # Allow manual override via env var
        env_key = f"VMIGRATE_NODE_IP_{node}"
        env_ip = _os.environ.get(env_key, "").strip()
        if env_ip:
            logger.info("Using env override %s=%s for node '%s'", env_key, env_ip, node)
            return env_ip

        # Collect all candidate IPs for this node
        candidates: list[str] = []
        api = self.get_api()
        try:
            nodes = api.nodes.get()  # type: ignore[union-attr]
            for n in nodes:
                if n.get("node") == node:
                    for field in ("ip", "address"):
                        val = str(n.get(field, "")).strip()
                        if _is_ip(val) and val not in candidates:
                            candidates.append(val)
            # Also try node network interfaces
            try:
                net = api.nodes(node).network.get()  # type: ignore[union-attr]
                for iface in net:
                    for field in ("address", "gateway"):
                        val = str(iface.get(field, "")).strip()
                        if _is_ip(val) and not val.startswith("127.") and val not in candidates:
                            candidates.append(val)
            except Exception:
                pass
        except Exception as exc:
            logger.warning("Could not query Proxmox API for node IPs: %s", exc)

        # DNS fallback
        try:
            resolved = _sock.gethostbyname(node)
            if _is_ip(resolved) and resolved not in candidates:
                candidates.append(resolved)
        except Exception:
            pass

        # Always include the API host as last resort
        if self._config.host not in candidates:
            candidates.append(self._config.host)

        # Add cluster IPs from UI form / env var
        cluster_ips_env = _os.environ.get("VMIGRATE_CLUSTER_IPS", "")
        for ip in cluster_ips_env.split(","):
            ip = ip.strip()
            if _is_ip(ip) and ip not in candidates:
                candidates.append(ip)

        # Add any extra candidates passed directly (from session/config)
        for ip in (extra_candidates or []):
            ip = ip.strip()
            if _is_ip(ip) and ip not in candidates:
                candidates.append(ip)

        logger.debug("IP candidates for node '%s': %s", node, candidates)

        # Return the first candidate reachable on SSH port 22
        for ip in candidates:
            if _reachable(ip, port=22, timeout=3.0):
                logger.info("Resolved node '%s' → %s (SSH reachable)", node, ip)
                return ip
            else:
                logger.debug("Node '%s' candidate %s is not reachable on :22, skipping.", node, ip)

        # Nothing reachable — fall back to API host and let SSH fail with a clear error
        logger.warning(
            "No reachable SSH IP found for node '%s' — using API host '%s'.",
            node, self._config.host,
        )
        return self._config.host

    def wait_for_task(self, node: str, upid: str, timeout: int = 120) -> None:
        """Block until a Proxmox task (UPID) completes.

        Args:
            node: Proxmox node name where the task is running.
            upid: Task UPID string returned by an API call.
            timeout: Max seconds to wait before raising RuntimeError.

        Raises:
            RuntimeError: If the task fails or times out.
        """
        import time as _time
        api = self.get_api()
        deadline = _time.time() + timeout
        while _time.time() < deadline:
            try:
                status = api.nodes(node).tasks(upid).status.get()  # type: ignore
                if status.get("status") == "stopped":
                    exit_status = status.get("exitstatus", "")
                    if exit_status == "OK":
                        return
                    raise RuntimeError(
                        f"Proxmox task {upid} failed with exitstatus='{exit_status}'"
                    )
            except RuntimeError:
                raise
            except Exception:
                pass  # task may not be queryable immediately
            _time.sleep(2)
        raise RuntimeError(
            f"Proxmox task {upid} did not complete within {timeout}s"
        )

    def get_next_vmid(self) -> int:
        """Return the next available VMID from the Proxmox cluster.

        Proxmox provides a ``/cluster/nextid`` endpoint that returns a safe
        VMID that is not already in use.

        Returns:
            Integer VMID >= 100.

        Raises:
            RuntimeError: If the API call fails.
        """
        api = self.get_api()
        try:
            vmid = int(api.cluster.nextid.get())  # type: ignore[union-attr]
            logger.debug("Next available Proxmox VMID: %d", vmid)
            return vmid
        except Exception as exc:
            raise RuntimeError(
                f"Failed to get next VMID from Proxmox: {exc}"
            ) from exc

    # ------------------------------------------------------------------
    # Context manager
    # ------------------------------------------------------------------

    def __enter__(self) -> "ProxmoxClient":
        self.connect()
        return self

    def __exit__(self, exc_type: object, exc_val: object, exc_tb: object) -> None:
        # proxmoxer does not require explicit disconnect
        self._api = None

    def __repr__(self) -> str:
        connected = self._api is not None
        return (
            f"ProxmoxClient(host={self._config.host!r}, "
            f"connected={connected})"
        )
