"""ZooKeeper 3.5.5 async client wrapper.

Bridges kazoo (synchronous, threaded) to asyncio:
- ``connect()`` runs ``kazoo.start`` in a thread executor
- State changes from kazoo's internal listener thread are routed back to
  the captured asyncio loop via ``run_coroutine_threadsafe``
- All callbacks must check ``loop.is_closed()`` because kazoo can fire
  during shutdown after the loop has stopped

ZK 3.5.5 quirks this wrapper compensates for:
- ``session_timeout`` must be in the [4 × tickTime, 20 × tickTime] range
  (typically 4-40 seconds for the default tickTime=2s)
- 4lw commands (``stat``, ``ruok``, ``conf``) are blocked by default and
  must be added to ``4lw.commands.whitelist`` in zoo.cfg. We treat any 4lw
  failure as "unknown" and never raise.
- Watches and ephemeral nodes are NOT auto-restored after a LOST session.
  Re-registration is the caller's responsibility (see PartitionManager).
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import structlog
from kazoo.client import KazooClient
from kazoo.retry import KazooRetry

from src.config.settings import AppSettings

logger = structlog.get_logger(__name__)

StateHandler = Callable[[Any], Awaitable[None]]


class ZKClient:
    def __init__(self, settings: AppSettings) -> None:
        self._settings = settings
        self._kazoo: KazooClient | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._state_handlers: list[StateHandler] = []

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------
    @property
    def kazoo(self) -> KazooClient:
        if self._kazoo is None:
            raise RuntimeError("ZKClient.connect() must be called first")
        return self._kazoo

    @property
    def loop(self) -> asyncio.AbstractEventLoop:
        if self._loop is None:
            raise RuntimeError("ZKClient.connect() must be called first")
        return self._loop

    @property
    def root_path(self) -> str:
        return self._settings.zk_root_path

    def is_connected(self) -> bool:
        return self._kazoo is not None and bool(self._kazoo.connected)

    # ------------------------------------------------------------------
    # Connect / close
    # ------------------------------------------------------------------
    async def connect(self) -> None:
        """Connect to ZK with a hard wall-clock budget.

        v6 P0-1: ``kazoo.start()`` is wrapped in ``asyncio.wait_for`` against a
        dedicated single-thread executor so a ZK outage cannot hang the lifespan
        forever. On budget exhaustion the executor is abandoned (daemon thread)
        and a ``TimeoutError`` is raised; ``init_infra`` then runs its normal
        ``close_partial`` rollback and the pod exits with a clear log.

        ``KazooRetry.max_delay`` is reduced from 30s to 5s so kazoo's internal
        backoff completes several attempts within the outer 45s budget instead
        of waiting half a minute between two retries.
        """
        self._loop = asyncio.get_running_loop()
        kwargs: dict[str, Any] = {
            "hosts": self._settings.zk_hosts,
            "timeout": self._settings.zk_session_timeout,
            "connection_retry": KazooRetry(
                max_tries=-1, delay=1, backoff=2, max_delay=5
            ),
            "command_retry": KazooRetry(max_tries=3, delay=0.5),
        }
        if self._settings.zk_sasl_mechanism:
            kwargs["sasl_options"] = {
                "mechanism": self._settings.zk_sasl_mechanism,
                "username": self._settings.zk_sasl_username,
                "password": self._settings.zk_sasl_password.get_secret_value(),
            }
        self._kazoo = KazooClient(**kwargs)
        self._kazoo.add_listener(self._state_listener)

        budget_sec = self._settings.zk_startup_budget_sec
        wall_start = time.monotonic()

        # Dedicated executor: if kazoo.start() hangs we must abandon the
        # thread without polluting the default executor pool. The thread is
        # a daemon, so the pod exit will reap it.
        start_executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="zk-startup"
        )
        try:
            future = start_executor.submit(self._kazoo.start)
            try:
                await asyncio.wait_for(
                    asyncio.wrap_future(future), timeout=budget_sec
                )
            except TimeoutError as e:
                elapsed = time.monotonic() - wall_start
                logger.error(
                    "zk_startup_timeout",
                    elapsed_sec=round(elapsed, 1),
                    budget_sec=budget_sec,
                    hosts=self._settings.zk_hosts,
                )
                raise TimeoutError(
                    f"zk_startup_budget_exceeded ({budget_sec}s)"
                ) from e
            except Exception as e:
                # Non-timeout kazoo.start() failures (AuthFailed, ConnectionRefused,
                # socket errors). Log hosts/error_type for fail-fast diagnosis, then
                # re-raise so init_infra's rollback runs as before.
                logger.error(
                    "zk_connect_failed",
                    hosts=self._settings.zk_hosts,
                    error_type=type(e).__name__,
                    error=str(e),
                )
                raise
        finally:
            # wait=False: on timeout we leak the daemon thread on purpose;
            # on success the thread has already returned.
            start_executor.shutdown(wait=False)

        await self._loop.run_in_executor(
            None, self._kazoo.ensure_path, self._settings.zk_root_path
        )
        elapsed = time.monotonic() - wall_start
        logger.info(
            "zk_connected",
            hosts=self._settings.zk_hosts,
            elapsed_sec=round(elapsed, 2),
        )

    async def close(self) -> None:
        if self._kazoo is None:
            return
        try:
            assert self._loop is not None
            await self._loop.run_in_executor(None, self._kazoo.stop)
            await self._loop.run_in_executor(None, self._kazoo.close)
        except Exception as e:
            logger.warning("zk_close_failed", error=str(e))
        finally:
            self._kazoo = None

    # ------------------------------------------------------------------
    # State bridge (kazoo thread → asyncio loop)
    # ------------------------------------------------------------------
    def add_state_handler(self, handler: StateHandler) -> None:
        self._state_handlers.append(handler)

    def _state_listener(self, state: Any) -> None:
        """Invoked by kazoo's internal listener thread.

        We are NOT on the asyncio loop here. We hop back via
        ``run_coroutine_threadsafe``. If the loop is closed (shutdown), we
        silently drop the event — kazoo may keep firing after our lifespan
        finishes and we don't want that to surface as an error.
        """
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        try:
            running = loop.is_running()
        except RuntimeError:
            return
        if not running:
            return
        for handler in self._state_handlers:
            try:
                future = asyncio.run_coroutine_threadsafe(handler(state), loop)
                future.add_done_callback(self._log_state_handler_exception)
            except Exception as e:
                # Defensive — run_coroutine_threadsafe itself failed
                logger.error(
                    "zk_state_handler_dispatch_failed",
                    error=str(e),
                    exc_info=True,
                )

    @staticmethod
    def _log_state_handler_exception(f: Any) -> None:
        if not f.cancelled():
            try:
                exc = f.exception()
            except Exception:
                return
            if exc is not None:
                logger.error(
                    "zk_state_handler_failed", error=str(exc), exc_info=exc
                )

    # ------------------------------------------------------------------
    # Server version (4lw stat, optional)
    # ------------------------------------------------------------------
    async def get_server_version(self) -> str:
        """Return the ZK server version, or ``"unknown"`` on any failure.

        We use the ``stat`` 4lw command, which on ZK 3.5.0+ is BLOCKED by
        default. Operators must add it to ``4lw.commands.whitelist`` in
        zoo.cfg. If the command fails, we log a warning and return
        ``"unknown"`` — never raise. The version is informational only;
        the service must keep running without it.
        """
        if self._kazoo is None or self._loop is None:
            return "unknown"
        try:
            stat_bytes: bytes = await asyncio.wait_for(
                self._loop.run_in_executor(
                    None, lambda: self._kazoo.command(b"stat")
                ),
                timeout=3.0,
            )
            text = (
                stat_bytes.decode("utf-8", errors="ignore")
                if isinstance(stat_bytes, bytes)
                else str(stat_bytes)
            )
            for line in text.split("\n"):
                if line.startswith("Zookeeper version:"):
                    return line.split(":", 1)[1].strip()
        except TimeoutError:
            logger.warning("zk_stat_command_timeout")
        except Exception as e:
            # ConnectionResetError = 4lw whitelist blocks the command
            logger.warning(
                "zk_stat_command_unavailable",
                error_type=type(e).__name__,
                hint="check 4lw.commands.whitelist in zoo.cfg",
            )
        return "unknown"


# Reduce noise from kazoo's chatty internal logger
logging.getLogger("kazoo").setLevel(logging.WARNING)
