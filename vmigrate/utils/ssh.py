"""SSH client wrapper for vmigrate.

Wraps Paramiko to provide a simple, testable interface for running commands
and transferring files to remote hosts (conversion host, Proxmox nodes).
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Optional, Tuple

import paramiko

logger = logging.getLogger("vmigrate.ssh")


class SSHClient:
    """Paramiko-backed SSH client with file transfer support.

    Supports both key-based and password-based authentication.  The
    connection is lazy - call :meth:`connect` before using other methods, or
    use the class as a context manager.

    Example::

        with SSHClient("host.example.com", "root", key_path="/root/.ssh/id_rsa") as ssh:
            rc, out, err = ssh.run("df -h")
    """

    def __init__(
        self,
        host: str,
        user: str,
        key_path: Optional[str] = None,
        password: Optional[str] = None,
        port: int = 22,
        connect_timeout: int = 30,
    ) -> None:
        """Initialise the SSH client parameters (does not connect yet).

        Args:
            host: Hostname or IP address of the remote machine.
            user: SSH username.
            key_path: Path to a private key file (PEM format).  If ``None``
                and ``password`` is also ``None``, the SSH agent and default
                key locations (~/.ssh/id_rsa etc.) are tried.
            password: Password for password-based auth (or passphrase for
                encrypted key).
            port: SSH port (default 22).
            connect_timeout: TCP connect timeout in seconds.
        """
        self.host = host
        self.user = user
        self.key_path = key_path
        self.password = password
        self.port = port
        self.connect_timeout = connect_timeout

        self._client: Optional[paramiko.SSHClient] = None

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------

    def connect(self) -> None:
        """Open the SSH connection.

        Raises:
            paramiko.AuthenticationException: If credentials are wrong.
            paramiko.SSHException: On other SSH-level errors.
            OSError: If the host is unreachable.
        """
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())  # type: ignore[no-untyped-call]

        connect_kwargs: dict = {
            "hostname": self.host,
            "port": self.port,
            "username": self.user,
            "timeout": self.connect_timeout,
        }

        if self.key_path:
            connect_kwargs["key_filename"] = str(self.key_path)
        if self.password:
            connect_kwargs["password"] = self.password
        if not self.key_path and not self.password:
            connect_kwargs["allow_agent"] = True
            connect_kwargs["look_for_keys"] = True

        logger.debug("SSH connecting to %s@%s:%d", self.user, self.host, self.port)
        client.connect(**connect_kwargs)
        # Send keepalive every 30 s so the server does not drop the connection
        # during long SFTP uploads (large VMDK files).
        transport = client.get_transport()
        if transport:
            transport.set_keepalive(30)
        self._client = client
        logger.debug("SSH connection established to %s", self.host)

    def _ensure_connected(self) -> None:
        """Raise RuntimeError if not connected."""
        if self._client is None:
            raise RuntimeError(
                "SSHClient is not connected. Call connect() or use as a context manager."
            )

    # ------------------------------------------------------------------
    # Command execution
    # ------------------------------------------------------------------

    def run(
        self,
        cmd: str,
        timeout: int = 300,
    ) -> Tuple[int, str, str]:
        """Execute a shell command on the remote host.

        Args:
            cmd: Shell command string to execute.
            timeout: Maximum seconds to wait for the command to finish.

        Returns:
            Tuple of (exit_code, stdout, stderr) where stdout and stderr are
            decoded strings with trailing whitespace stripped.

        Raises:
            RuntimeError: If not connected.
            paramiko.SSHException: On channel errors.
        """
        self._ensure_connected()
        assert self._client is not None  # mypy

        logger.debug("SSH run [%s]: %s", self.host, cmd)
        stdin, stdout, stderr = self._client.exec_command(cmd, timeout=timeout)
        exit_code = stdout.channel.recv_exit_status()
        stdout_str = stdout.read().decode("utf-8", errors="replace").rstrip()
        stderr_str = stderr.read().decode("utf-8", errors="replace").rstrip()

        if exit_code != 0:
            logger.debug(
                "SSH command exited %d on %s:\n  cmd: %s\n  stderr: %s",
                exit_code,
                self.host,
                cmd,
                stderr_str[:500],
            )
        return exit_code, stdout_str, stderr_str

    # ------------------------------------------------------------------
    # File transfer
    # ------------------------------------------------------------------

    def put_file(self, local: Path, remote: str) -> None:
        """Upload a local file to a remote path.

        Args:
            local: Local file path (must exist).
            remote: Absolute remote path to write to.

        Raises:
            FileNotFoundError: If the local file does not exist.
            RuntimeError: If not connected.
        """
        self._ensure_connected()
        assert self._client is not None

        if not local.exists():
            raise FileNotFoundError(f"Local file not found for upload: {local}")

        # Use a larger window size for faster bulk transfers
        transport = self._client.get_transport()
        if transport:
            transport.default_window_size = 64 * 1024 * 1024   # 64 MB
            transport.packetizer.REKEY_BYTES = pow(2, 40)       # disable rekey mid-transfer
            transport.packetizer.REKEY_PACKETS = pow(2, 40)

        sftp = self._client.open_sftp()
        sftp.get_channel().settimeout(3600)   # 1 h timeout for the SFTP channel
        try:
            logger.debug("SFTP put %s -> %s:%s", local, self.host, remote)
            file_size = local.stat().st_size
            transferred = 0

            def _progress(sent: int, total: int) -> None:
                nonlocal transferred
                transferred = sent
                if total > 0 and sent % (100 * 1024 * 1024) < 32768:   # log every ~100 MB
                    pct = int(sent / total * 100)
                    logger.info(
                        "  SFTP upload %s: %d%%  %.0f/%.0f MB",
                        local.name, pct, sent / 1024 / 1024, total / 1024 / 1024,
                    )

            sftp.put(str(local), remote, callback=_progress, confirm=True)
        finally:
            sftp.close()

    def get_file(self, remote: str, local: Path) -> None:
        """Download a remote file to a local path.

        Args:
            remote: Absolute remote file path.
            local: Local destination path (parent must exist).

        Raises:
            RuntimeError: If not connected.
        """
        self._ensure_connected()
        assert self._client is not None

        local.parent.mkdir(parents=True, exist_ok=True)
        sftp = self._client.open_sftp()
        try:
            logger.debug("SFTP get %s:%s -> %s", self.host, remote, local)
            sftp.get(remote, str(local))
        finally:
            sftp.close()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def close(self) -> None:
        """Close the SSH connection if open."""
        if self._client is not None:
            logger.debug("SSH closing connection to %s", self.host)
            self._client.close()
            self._client = None

    # ------------------------------------------------------------------
    # Context manager
    # ------------------------------------------------------------------

    def __enter__(self) -> "SSHClient":
        self.connect()
        return self

    def __exit__(self, exc_type: object, exc_val: object, exc_tb: object) -> None:
        self.close()

    def __repr__(self) -> str:
        return f"SSHClient(host={self.host!r}, user={self.user!r}, port={self.port})"
