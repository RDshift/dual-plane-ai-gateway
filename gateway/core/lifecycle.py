"""Process hygiene, signal trapping, and deterministic resource cleanup."""

import asyncio
import signal
from collections.abc import Callable, Coroutine
from typing import Any
from gateway.core.logging import configure_stderr_logging

logger = configure_stderr_logging(logger_name="quilr.lifecycle")


class LifecycleManager:
    """Manages startup verification and clean shutdown hooks."""

    def __init__(self) -> None:
        self._shutdown_hooks: list[Callable[[], Coroutine[Any, Any, None]]] = []
        self._is_shutting_down: bool = False

    def register_shutdown_hook(
        self, hook: Callable[[], Coroutine[Any, Any, None]]
    ) -> None:
        """Register an async cleanup coroutine for teardown."""
        self._shutdown_hooks.append(hook)

    async def execute_shutdown(self) -> None:
        """Run all registered cleanup hooks idempotently."""
        if self._is_shutting_down:
            return
        self._is_shutting_down = True
        logger.info("Executing deterministic substrate shutdown hooks.")
        for hook in reversed(self._shutdown_hooks):
            try:
                await hook()
            except Exception as exc:
                logger.error(f"Shutdown hook error: {exc}")

    def install_signal_handlers(self, loop: asyncio.AbstractEventLoop) -> None:
        """Bind POSIX SIGINT, SIGTERM, and SIGHUP signals."""
        signals = [signal.SIGINT, signal.SIGTERM]
        if hasattr(signal, "SIGHUP"):
            signals.append( getattr(signal, "SIGHUP") )
        for sig in signals:
            try:
                loop.add_signal_handler(
                    sig,
                    lambda s=sig: asyncio.create_task(self._handle_signal(s)),
                )
            except (NotImplementedError, RuntimeError):
                pass

    async def _handle_signal(self, sig: signal.Signals) -> None:
        logger.info(f"Intercepted POSIX signal {sig.name}; initiating clean exit.")
        await self.execute_shutdown()


lifecycle_manager = LifecycleManager()