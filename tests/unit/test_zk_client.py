"""Tests for src.distributed.zk_client (ZK 3.5.5 wrapper).

The tricky part of testing this module is the kazoo→asyncio bridge:
kazoo invokes listeners from its own internal thread, and we have to hop
back to the asyncio loop. We use `threading.Event` to deterministically
wait for the bridged coroutine to actually run.
"""
import asyncio
import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from src.config.settings import AppSettings
from src.distributed.zk_client import ZKClient


@pytest.fixture
def settings() -> AppSettings:
    return AppSettings(
        zk_hosts="zk1:2181",
        zk_root_path="/resource-monitor",
        zk_session_timeout=30,
    )


@pytest.fixture
def settings_sasl() -> AppSettings:
    return AppSettings(
        zk_hosts="zk1:2181",
        zk_root_path="/resource-monitor",
        zk_sasl_mechanism="DIGEST-MD5",
        zk_sasl_username="monitor",
        zk_sasl_password="zksecret",
    )


# ----------------------------------------------------------------------
# Connect
# ----------------------------------------------------------------------
@pytest.mark.unit
class TestZKClientConnect:
    async def test_connect_passes_session_timeout(self, settings):
        client = ZKClient(settings)
        with patch("src.distributed.zk_client.KazooClient") as mock_cls:
            instance = MagicMock()
            mock_cls.return_value = instance
            await client.connect()
        kwargs = mock_cls.call_args.kwargs
        assert kwargs["timeout"] == 30

    async def test_connect_passes_hosts(self, settings):
        client = ZKClient(settings)
        with patch("src.distributed.zk_client.KazooClient") as mock_cls:
            instance = MagicMock()
            mock_cls.return_value = instance
            await client.connect()
        kwargs = mock_cls.call_args.kwargs
        assert kwargs["hosts"] == "zk1:2181"

    async def test_connect_omits_sasl_when_mechanism_empty(self, settings):
        client = ZKClient(settings)
        with patch("src.distributed.zk_client.KazooClient") as mock_cls:
            instance = MagicMock()
            mock_cls.return_value = instance
            await client.connect()
        kwargs = mock_cls.call_args.kwargs
        assert "sasl_options" not in kwargs

    async def test_connect_includes_sasl_when_mechanism_set(self, settings_sasl):
        client = ZKClient(settings_sasl)
        with patch("src.distributed.zk_client.KazooClient") as mock_cls:
            instance = MagicMock()
            mock_cls.return_value = instance
            await client.connect()
        kwargs = mock_cls.call_args.kwargs
        assert "sasl_options" in kwargs
        assert kwargs["sasl_options"]["mechanism"] == "DIGEST-MD5"
        assert kwargs["sasl_options"]["username"] == "monitor"
        assert kwargs["sasl_options"]["password"] == "zksecret"

    async def test_connect_starts_kazoo_and_ensures_root_path(self, settings):
        client = ZKClient(settings)
        with patch("src.distributed.zk_client.KazooClient") as mock_cls:
            instance = MagicMock()
            mock_cls.return_value = instance
            await client.connect()
        instance.start.assert_called_once()
        instance.ensure_path.assert_called_once_with("/resource-monitor")


# ----------------------------------------------------------------------
# State listener bridge
# ----------------------------------------------------------------------
@pytest.mark.unit
class TestStateListenerBridge:
    async def test_kazoo_thread_callback_reaches_asyncio_loop(self, settings):
        """A state change from a NON-asyncio thread must invoke the async handler
        on the captured loop. We use threading.Event to wait deterministically."""
        client = ZKClient(settings)
        client._loop = asyncio.get_running_loop()

        done = threading.Event()
        received_state = []

        async def handler(state):
            received_state.append(state)
            done.set()

        client.add_state_handler(handler)

        # Simulate kazoo invoking the listener from its own thread
        def fire_from_other_thread():
            client._state_listener("CONNECTED")

        thread = threading.Thread(target=fire_from_other_thread)
        thread.start()
        thread.join()

        # The asyncio loop must run pending tasks for the handler to execute
        await asyncio.sleep(0.05)
        assert done.is_set()
        assert received_state == ["CONNECTED"]

    async def test_listener_no_loop_does_not_crash(self, settings):
        """Bridging without a running loop must be a safe no-op (e.g. during shutdown)."""
        client = ZKClient(settings)
        client._loop = None  # not connected
        # Must not raise
        client._state_listener("LOST")

    async def test_multiple_handlers_all_receive_state(self, settings):
        client = ZKClient(settings)
        client._loop = asyncio.get_running_loop()

        results = []
        done1 = threading.Event()
        done2 = threading.Event()

        async def h1(s):
            results.append(("h1", s))
            done1.set()

        async def h2(s):
            results.append(("h2", s))
            done2.set()

        client.add_state_handler(h1)
        client.add_state_handler(h2)

        threading.Thread(target=lambda: client._state_listener("SUSPENDED")).start()
        await asyncio.sleep(0.05)
        assert done1.is_set() and done2.is_set()
        assert ("h1", "SUSPENDED") in results
        assert ("h2", "SUSPENDED") in results


# ----------------------------------------------------------------------
# is_connected, root_path
# ----------------------------------------------------------------------
@pytest.mark.unit
class TestZKClientProperties:
    async def test_is_connected_when_kazoo_connected(self, settings):
        client = ZKClient(settings)
        instance = MagicMock()
        instance.connected = True
        client._kazoo = instance
        assert client.is_connected() is True

    async def test_is_connected_when_kazoo_disconnected(self, settings):
        client = ZKClient(settings)
        instance = MagicMock()
        instance.connected = False
        client._kazoo = instance
        assert client.is_connected() is False

    async def test_is_connected_when_not_connected(self, settings):
        client = ZKClient(settings)
        assert client.is_connected() is False

    def test_root_path_from_settings(self, settings):
        client = ZKClient(settings)
        assert client.root_path == "/resource-monitor"


# ----------------------------------------------------------------------
# Startup budget (P0-1) — ZK must not hang the lifespan forever
# ----------------------------------------------------------------------
@pytest.mark.unit
class TestZKClientStartupBudget:
    """v6 P0-1: kazoo.start() must be capped by zk_startup_budget_sec.

    Without this cap, KazooRetry(max_tries=-1) hangs init_infra forever
    when ZK is unreachable, which prevents lifespan from yielding and
    therefore makes /healthz/live unreachable until K8s liveness fires
    at t=60s. The pod ends up in CrashLoopBackoff with minimal signal.
    """

    async def test_connect_raises_timeout_when_kazoo_start_hangs(self):
        """kazoo.start() simulating a hang must be aborted within budget."""
        settings = AppSettings(
            zk_hosts="zk1:2181",
            zk_root_path="/resource-monitor",
            zk_startup_budget_sec=1,  # short budget for the test
        )
        client = ZKClient(settings)

        def blocking_start() -> None:
            time.sleep(10)

        with patch("src.distributed.zk_client.KazooClient") as mock_cls:
            instance = MagicMock()
            instance.start.side_effect = blocking_start
            mock_cls.return_value = instance

            wall_start = time.monotonic()
            with pytest.raises(TimeoutError, match="zk_startup_budget"):
                await client.connect()
            elapsed = time.monotonic() - wall_start

        # 1s budget + small overhead. The 10s sleep MUST NOT be reached.
        assert elapsed < 3.0, (
            f"connect() did not respect zk_startup_budget_sec: {elapsed:.2f}s"
        )

    async def test_connect_uses_reduced_internal_max_delay(self, settings):
        """KazooRetry max_delay must be 5 (was 30) so internal retries
        don't dominate the outer 45s budget."""
        client = ZKClient(settings)
        with patch("src.distributed.zk_client.KazooClient") as mock_cls:
            instance = MagicMock()
            mock_cls.return_value = instance
            await client.connect()
        kwargs = mock_cls.call_args.kwargs
        retry = kwargs["connection_retry"]
        assert retry.max_delay == 5

    async def test_connect_succeeds_when_kazoo_returns_promptly(self, settings):
        """Happy path: budget should not trip when kazoo.start is fast."""
        client = ZKClient(settings)
        with patch("src.distributed.zk_client.KazooClient") as mock_cls:
            instance = MagicMock()  # default Mock returns instantly
            mock_cls.return_value = instance
            await client.connect()
        instance.start.assert_called_once()
        instance.ensure_path.assert_called_once_with("/resource-monitor")


# ----------------------------------------------------------------------
# get_server_version (4lw fallback)
# ----------------------------------------------------------------------
@pytest.mark.unit
class TestGetServerVersion:
    async def test_parses_stat_output(self, settings):
        client = ZKClient(settings)
        client._loop = asyncio.get_running_loop()
        instance = MagicMock()
        instance.command.return_value = (
            b"Zookeeper version: 3.5.5-snapshot, built on 2019-XX-XX\n"
            b"Clients:\n"
        )
        client._kazoo = instance
        version = await client.get_server_version()
        assert "3.5.5" in version

    async def test_returns_unknown_on_4lw_blocked(self, settings):
        """ZK 3.5.0+ blocks 4lw commands by default — we must NOT raise."""
        client = ZKClient(settings)
        client._loop = asyncio.get_running_loop()
        instance = MagicMock()
        instance.command.side_effect = ConnectionResetError("blocked")
        client._kazoo = instance
        version = await client.get_server_version()
        assert version == "unknown"

    async def test_returns_unknown_on_unknown_format(self, settings):
        client = ZKClient(settings)
        client._loop = asyncio.get_running_loop()
        instance = MagicMock()
        instance.command.return_value = b"some unparseable output\n"
        client._kazoo = instance
        version = await client.get_server_version()
        assert version == "unknown"
