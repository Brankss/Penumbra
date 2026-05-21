"""Tor controller — manage a local Tor process and circuit rotation.

This module talks to Tor over its ControlPort (via the `stem` library) and exposes
an async API so it can plug into Penumbra's asyncio pipeline. Tor itself is a
synchronous process; the blocking calls are offloaded to an executor.

Penumbra intentionally **does not bundle** Tor — the user installs it via their
OS package manager. We start, control, and stop a local Tor instance configured
just for our session, with isolated data directory and ephemeral ports.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import shutil
import socket
import tempfile
from pathlib import Path
from typing import Any

import httpx

from penumbra.exceptions import TorError

logger = logging.getLogger(__name__)


def _pick_free_port() -> int:
    """Ask the OS for a free TCP port (race-prone but fine for ephemeral binding)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _locate_tor_binary(explicit: str | None = None) -> str:
    """Resolve the Tor executable. Honors explicit override, then PATH."""
    if explicit:
        if not Path(explicit).is_file():
            raise TorError(f"Tor binary not found at: {explicit}")
        return explicit
    found = shutil.which("tor") or shutil.which("tor.exe")
    if not found:
        raise TorError(
            "Tor not found on PATH. Install via your package manager:\n"
            "  Windows:  choco install tor\n"
            "  macOS:    brew install tor\n"
            "  Debian:   sudo apt install tor\n"
            "Or pass `tor_binary='...'` explicitly."
        )
    return found


class TorController:
    """Lifecycle manager for a private Tor process used during a research session.

    Designed as an async context manager:

        async with TorController() as tor:
            print(tor.socks_proxy_url)         # socks5h://127.0.0.1:<port>
            await tor.new_circuit()            # rotate exit node
    """

    def __init__(
        self,
        *,
        tor_binary: str | None = None,
        data_dir: Path | None = None,
        startup_timeout: float = 60.0,
        circuit_timeout: float = 30.0,
    ) -> None:
        self._binary = _locate_tor_binary(tor_binary)
        self._data_dir = data_dir
        self._owns_data_dir = data_dir is None
        self._startup_timeout = startup_timeout
        self._circuit_timeout = circuit_timeout
        self._process: Any | None = None
        self._controller: Any | None = None
        self.socks_port: int | None = None
        self.control_port: int | None = None

    @property
    def socks_proxy_url(self) -> str:
        if self.socks_port is None:
            raise TorError("Tor is not running — call .start() first.")
        return f"socks5h://127.0.0.1:{self.socks_port}"

    @property
    def is_running(self) -> bool:
        return self._process is not None and self._controller is not None

    async def __aenter__(self) -> TorController:
        await self.start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.stop()

    async def start(self) -> None:
        """Boot the Tor process and connect to its control port."""
        if self.is_running:
            return
        try:
            from stem.process import launch_tor_with_config
        except ImportError as e:
            raise TorError("`stem` is not installed; reinstall penumbra.") from e

        self.socks_port = _pick_free_port()
        self.control_port = _pick_free_port()
        if self._owns_data_dir:
            self._data_dir = Path(tempfile.mkdtemp(prefix="penumbra-tor-"))

        config = {
            "SocksPort": str(self.socks_port),
            "ControlPort": str(self.control_port),
            "DataDirectory": str(self._data_dir),
            "CookieAuthentication": "1",
            "Log": "notice stdout",
            "ClientUseIPv6": "1",
            "AvoidDiskWrites": "1",
        }

        loop = asyncio.get_running_loop()

        def _launch() -> Any:
            return launch_tor_with_config(
                config=config,
                tor_cmd=self._binary,
                init_msg_handler=lambda line: logger.debug("tor: %s", line),
                timeout=self._startup_timeout,
                take_ownership=True,
            )

        try:
            self._process = await loop.run_in_executor(None, _launch)
        except Exception as e:
            await self._cleanup_data_dir()
            raise TorError(f"Failed to launch Tor: {e}") from e

        await self._connect_controller()
        logger.info("Tor started — SOCKS=%d CONTROL=%d", self.socks_port, self.control_port)

    async def _connect_controller(self) -> None:
        from stem.control import Controller

        loop = asyncio.get_running_loop()

        def _connect() -> Any:
            ctrl = Controller.from_port(port=self.control_port)
            ctrl.authenticate()
            return ctrl

        try:
            self._controller = await loop.run_in_executor(None, _connect)
        except Exception as e:
            raise TorError(f"Failed to authenticate to Tor control port: {e}") from e

    async def new_circuit(self) -> None:
        """Force Tor to build a fresh circuit (i.e. rotate exit node)."""
        if not self.is_running or self._controller is None:
            raise TorError("Tor is not running.")
        loop = asyncio.get_running_loop()

        def _signal() -> None:
            from stem import Signal

            self._controller.signal(Signal.NEWNYM)

        try:
            await asyncio.wait_for(
                loop.run_in_executor(None, _signal),
                timeout=self._circuit_timeout,
            )
            await asyncio.sleep(self._controller.get_newnym_wait())
        except asyncio.TimeoutError as e:
            raise TorError("Circuit rotation timed out.") from e
        logger.debug("Tor circuit rotated.")

    async def verify_connectivity(self) -> str:
        """Confirm the exit IP differs from the local one. Returns the exit IP."""
        if not self.is_running:
            raise TorError("Tor is not running.")
        async with httpx.AsyncClient(
            proxy=self.socks_proxy_url,
            timeout=30.0,
        ) as client:
            try:
                resp = await client.get("https://check.torproject.org/api/ip")
                resp.raise_for_status()
                data = resp.json()
            except (httpx.HTTPError, ValueError) as e:
                raise TorError(f"Tor connectivity check failed: {e}") from e
        if not data.get("IsTor", False):
            raise TorError("Connection is NOT going through Tor — refusing to proceed.")
        return data.get("IP", "unknown")

    async def stop(self) -> None:
        """Tear down the Tor process and clean up the data directory."""
        if self._controller is not None:
            with contextlib.suppress(Exception):
                self._controller.close()
            self._controller = None
        if self._process is not None:
            with contextlib.suppress(Exception):
                self._process.kill()
                self._process.wait(timeout=10)
            self._process = None
        await self._cleanup_data_dir()
        self.socks_port = None
        self.control_port = None
        logger.info("Tor stopped.")

    async def _cleanup_data_dir(self) -> None:
        if self._owns_data_dir and self._data_dir is not None:
            with contextlib.suppress(Exception):
                shutil.rmtree(self._data_dir)
            self._data_dir = None
