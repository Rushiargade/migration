"""VMware vSphere connection client for vmigrate.

Manages a pyVmomi ServiceInstance connection with automatic keepalive so that
long-running migrations do not time out their vCenter session.
"""

from __future__ import annotations

import logging
import ssl
import threading
import time
from typing import Optional

from pyVim.connect import Disconnect, SmartConnect  # type: ignore
from pyVmomi import vim  # type: ignore

from vmigrate.config import VMwareConfig

logger = logging.getLogger("vmigrate.vmware.client")

_KEEPALIVE_INTERVAL_SECONDS = 20 * 60  # 20 minutes


class VMwareClient:
    """Manages a pyVmomi connection to VMware vCenter or ESXi.

    Starts a background keepalive thread after connecting so that the session
    does not expire during long disk exports.

    Example::

        with VMwareClient(config.vmware) as client:
            si = client.get_service_instance()
    """

    def __init__(self, config: VMwareConfig) -> None:
        """Initialise with VMware connection configuration.

        Args:
            config: Populated :class:`VMwareConfig` dataclass.
        """
        self._config = config
        self._si: Optional[object] = None  # vim.ServiceInstance
        self._keepalive_thread: Optional[threading.Thread] = None
        self._stop_keepalive = threading.Event()

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------

    def connect(self) -> None:
        """Establish a connection to vCenter/ESXi.

        Uses ``SmartConnectNoSSL`` when ``verify_ssl=False`` (development) and
        ``SmartConnect`` with the system CA bundle otherwise.

        Raises:
            Exception: On connection or authentication failure.
        """
        logger.info(
            "Connecting to VMware %s:%d as %s",
            self._config.host,
            self._config.port,
            self._config.username,
        )
        if not self._config.verify_ssl:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
        else:
            context = ssl.create_default_context()

        si = SmartConnect(
            host=self._config.host,
            port=self._config.port,
            user=self._config.username,
            pwd=self._config.password,
            sslContext=context,
        )

        self._si = si
        logger.info("Connected to VMware %s", self._config.host)
        self._start_keepalive()

    def disconnect(self) -> None:
        """Disconnect from vCenter/ESXi and stop the keepalive thread."""
        self._stop_keepalive.set()
        if self._keepalive_thread is not None:
            self._keepalive_thread.join(timeout=5)
            self._keepalive_thread = None

        if self._si is not None:
            try:
                Disconnect(self._si)  # type: ignore[arg-type]
                logger.debug("Disconnected from VMware %s", self._config.host)
            except Exception as exc:
                logger.warning("Error during VMware disconnect: %s", exc)
            finally:
                self._si = None

    # ------------------------------------------------------------------
    # Keepalive
    # ------------------------------------------------------------------

    def _start_keepalive(self) -> None:
        """Start the background keepalive thread."""
        self._stop_keepalive.clear()
        self._keepalive_thread = threading.Thread(
            target=self._keepalive_loop,
            name="vmware-keepalive",
            daemon=True,
        )
        self._keepalive_thread.start()

    def _keepalive_loop(self) -> None:
        """Background loop that pings the session manager every 20 minutes."""
        while not self._stop_keepalive.wait(timeout=_KEEPALIVE_INTERVAL_SECONDS):
            try:
                if self._si is not None:
                    self._si.content.sessionManager.KeepAlive()  # type: ignore[union-attr]
                    logger.debug("VMware session keepalive sent to %s", self._config.host)
            except Exception as exc:
                logger.warning("VMware keepalive failed: %s", exc)

    # ------------------------------------------------------------------
    # Service instance access
    # ------------------------------------------------------------------

    def get_service_instance(self) -> object:  # -> vim.ServiceInstance
        """Return the active pyVmomi ServiceInstance.

        Returns:
            The ``vim.ServiceInstance`` object.

        Raises:
            RuntimeError: If not connected.
        """
        if self._si is None:
            raise RuntimeError(
                "VMwareClient is not connected. "
                "Call connect() or use as a context manager."
            )
        return self._si

    # ------------------------------------------------------------------
    # Task monitoring
    # ------------------------------------------------------------------

    def wait_for_task(self, task: object, timeout: int = 3600) -> bool:
        """Poll a vSphere task until it completes or times out.

        Logs progress percentage updates as they become available.

        Args:
            task: A ``vim.Task`` object returned by a vSphere API call.
            timeout: Maximum seconds to wait.

        Returns:
            ``True`` if the task succeeded, ``False`` if it failed or timed
            out.
        """
        deadline = time.time() + timeout
        last_pct: Optional[int] = None

        while time.time() < deadline:
            info = task.info  # type: ignore[union-attr]
            state = info.state

            if info.progress is not None and info.progress != last_pct:
                last_pct = info.progress
                logger.debug("Task %s progress: %d%%", info.key, info.progress)

            if state == vim.TaskInfo.State.success:
                logger.debug("Task %s completed successfully", info.key)
                return True
            elif state == vim.TaskInfo.State.error:
                error_msg = str(info.error.msg) if info.error else "Unknown error"
                logger.error(
                    "Task %s failed: %s", info.key, error_msg
                )
                return False

            time.sleep(2)

        logger.error("Task timed out after %d seconds", timeout)
        return False

    # ------------------------------------------------------------------
    # Context manager
    # ------------------------------------------------------------------

    def __enter__(self) -> "VMwareClient":
        self.connect()
        return self

    def __exit__(self, exc_type: object, exc_val: object, exc_tb: object) -> None:
        self.disconnect()

    def __repr__(self) -> str:
        connected = self._si is not None
        return (
            f"VMwareClient(host={self._config.host!r}, "
            f"connected={connected})"
        )
