"""Async stdio client bridge to the downstream MCP server process.

Interface: McpStdioBridge.exchange(request) writes one newline-delimited
JSON-RPC 2.0 frame to the child stdin and returns the matching response frame.
The module-level stdio_bridge singleton is shared by the router and the tests.

Dependencies: gateway.core.config.settings and
gateway.core.lifecycle.lifecycle_manager.
"""

import asyncio
import contextlib
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

from gateway.core.config import settings
from gateway.core.lifecycle import lifecycle_manager

PROJECT_ROOT = Path(__file__).resolve().parents[2]
STREAM_LIMIT_BYTES = 1024 * 1024
GRACEFUL_EXIT_TIMEOUT_S = 2.0
TERMINATE_TIMEOUT_S = 2.0
FRAME_ENCODING = "utf-8"

logger = logging.getLogger(__name__)


class BridgeError(Exception):
    """Base class for failures talking to the downstream MCP server."""


class BridgeTimeoutError(BridgeError):
    """Raised when the downstream server does not answer within the timeout."""


class BridgeUnavailableError(BridgeError):
    """Raised when the downstream server cannot be started or has died."""


class McpStdioBridge:
    """Serialized newline-delimited JSON-RPC client over a child process pipe."""

    def __init__(self) -> None:
        self.downstream_call_count: int = 0
        self._process: asyncio.subprocess.Process | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._lock: asyncio.Lock | None = None
        self._hook_registered: bool = False

    @property
    def script_path(self) -> Path:
        """Resolve the configured MCP server script against the project root."""
        path = Path(settings.mcp_server_script)
        return path if path.is_absolute() else PROJECT_ROOT / path

    @property
    def timeout_seconds(self) -> float:
        """Return the per-exchange upstream timeout in seconds."""
        return settings.upstream_timeout_ms / 1000.0

    async def exchange(self, request: dict[str, Any]) -> dict[str, Any]:
        """Send one JSON-RPC request and return the response with the same id.

        Raises BridgeTimeoutError or BridgeUnavailableError on failure. After
        any failure the child process is discarded so a late response can
        never be paired with a later request.
        """
        if "id" not in request:
            raise ValueError("exchange requires a request id")
        lock = self._bind_loop()
        frame = (json.dumps(request, separators=(",", ":")) + "\n").encode(
            FRAME_ENCODING
        )
        async with lock:
            process = await self._ensure_started()
            try:
                return await asyncio.wait_for(
                    self._roundtrip(process, frame, request["id"]),
                    timeout=self.timeout_seconds,
                )
            except TimeoutError as exc:
                logger.warning("event=mcp_bridge_timeout pid=%s", process.pid)
                await self._discard_process()
                raise BridgeTimeoutError("Downstream MCP server timed out") from exc
            except BridgeError:
                await self._discard_process()
                raise
            except (OSError, asyncio.IncompleteReadError, ValueError) as exc:
                logger.warning(
                    "event=mcp_bridge_io_error pid=%s error=%s", process.pid, exc
                )
                await self._discard_process()
                raise BridgeUnavailableError(
                    "Downstream MCP server connection failed"
                ) from exc
            except asyncio.CancelledError:
                self._abandon_process()
                raise

    async def shutdown(self) -> None:
        """Terminate and reap the child process. Safe to call repeatedly."""
        if self._loop is not asyncio.get_running_loop():
            self._abandon_process()
            return
        await self._discard_process()

    def _bind_loop(self) -> asyncio.Lock:
        loop = asyncio.get_running_loop()
        if self._loop is not loop or self._lock is None:
            self._abandon_process()
            self._loop = loop
            self._lock = asyncio.Lock()
        return self._lock

    async def _ensure_started(self) -> asyncio.subprocess.Process:
        process = self._process
        if process is not None and process.returncode is None:
            return process
        script = self.script_path
        if not script.is_file():
            raise BridgeUnavailableError(f"MCP server script not found: {script}")
        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"
        env["PYTHONPATH"] = str(PROJECT_ROOT)
        try:
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                str(script),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                cwd=str(PROJECT_ROOT),
                env=env,
                limit=STREAM_LIMIT_BYTES,
            )
        except OSError as exc:
            raise BridgeUnavailableError("Unable to start MCP server") from exc
        self._process = process
        self._register_hook()
        logger.info("event=mcp_bridge_started pid=%s script=%s", process.pid, script)
        return process

    def _register_hook(self) -> None:
        if not self._hook_registered:
            lifecycle_manager.register_shutdown_hook(self.shutdown)
            self._hook_registered = True

    async def _roundtrip(
        self,
        process: asyncio.subprocess.Process,
        frame: bytes,
        expected_id: Any,
    ) -> dict[str, Any]:
        stdin = process.stdin
        stdout = process.stdout
        if stdin is None or stdout is None:
            raise BridgeUnavailableError("MCP server pipes are not available")
        stdin.write(frame)
        self.downstream_call_count += 1
        await stdin.drain()
        while True:
            line = await stdout.readline()
            if not line:
                raise BridgeUnavailableError("MCP server closed stdout")
            text = line.strip()
            if not text:
                continue
            try:
                message = json.loads(text)
            except ValueError as exc:
                raise BridgeUnavailableError(
                    "MCP server emitted a non-JSON line on stdout"
                ) from exc
            if isinstance(message, dict) and message.get("id") == expected_id:
                return message

    async def _discard_process(self) -> None:
        process, self._process = self._process, None
        if process is not None:
            await self._reap(process)

    def _abandon_process(self) -> None:
        process, self._process = self._process, None
        if process is not None and process.returncode is None:
            with contextlib.suppress(Exception):
                process.kill()

    @staticmethod
    async def _reap(process: asyncio.subprocess.Process) -> None:
        if process.returncode is not None:
            return
        if process.stdin is not None:
            with contextlib.suppress(OSError, RuntimeError):
                process.stdin.close()
        try:
            await asyncio.wait_for(process.wait(), timeout=GRACEFUL_EXIT_TIMEOUT_S)
            return
        except TimeoutError:
            pass
        with contextlib.suppress(ProcessLookupError):
            process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=TERMINATE_TIMEOUT_S)
            return
        except TimeoutError:
            pass
        with contextlib.suppress(ProcessLookupError):
            process.kill()
        await process.wait()


stdio_bridge = McpStdioBridge()
