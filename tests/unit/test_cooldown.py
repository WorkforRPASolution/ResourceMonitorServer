"""Tests for src.cache.cooldown (AlertCooldownManager)."""
from unittest.mock import AsyncMock, MagicMock

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import TimeoutError as RedisTimeoutError

from src.cache.cooldown import AlertCooldownManager
from src.config.settings import AppSettings


@pytest.fixture
def mock_redis_client() -> MagicMock:
    """A RedisClient with an AsyncMock `.client` attribute."""
    rc = MagicMock()
    rc.key_prefix = "RESOURCE_ALERT"
    rc.client = AsyncMock()
    return rc


@pytest.fixture
def cooldown(mock_redis_client) -> AlertCooldownManager:
    return AlertCooldownManager(mock_redis_client)


# ----------------------------------------------------------------------
# is_cooling_down — single key
# ----------------------------------------------------------------------
@pytest.mark.unit
class TestIsCoolingDown:
    async def test_true_when_redis_key_exists(self, cooldown, mock_redis_client):
        mock_redis_client.client.exists.return_value = 1
        assert await cooldown.is_cooling_down("E1", "cpu", "total_used_pct") is True

    async def test_false_when_redis_key_missing(self, cooldown, mock_redis_client):
        mock_redis_client.client.exists.return_value = 0
        assert await cooldown.is_cooling_down("E1", "cpu", "total_used_pct") is False

    async def test_key_format_uses_prefix_category_metric(
        self, cooldown, mock_redis_client
    ):
        mock_redis_client.client.exists.return_value = 0
        await cooldown.is_cooling_down("E1", "cpu", "total_used_pct")
        key = mock_redis_client.client.exists.call_args.args[0]
        assert key == "RESOURCE_ALERT:cooldown:E1:cpu:total_used_pct"

    async def test_redis_connection_error_falls_back_to_local(
        self, cooldown, mock_redis_client
    ):
        """When Redis is down, consult the local TTLCache, NOT return False blindly."""
        # Pre-seed the local cache to indicate the alert is still cooling down.
        cooldown._local["RESOURCE_ALERT:cooldown:E1:cpu:total_used_pct"] = 1
        mock_redis_client.client.exists.side_effect = RedisConnectionError("boom")
        # Must return True (local says we're cooling), NOT False (which would flood email)
        assert await cooldown.is_cooling_down("E1", "cpu", "total_used_pct") is True

    async def test_redis_down_no_local_entry_returns_false(
        self, cooldown, mock_redis_client
    ):
        """First-ever alert during a Redis outage: local cache empty → allow send."""
        mock_redis_client.client.exists.side_effect = RedisConnectionError("boom")
        assert await cooldown.is_cooling_down("E1", "cpu", "total_used_pct") is False

    async def test_redis_timeout_falls_back_to_local(
        self, cooldown, mock_redis_client
    ):
        cooldown._local["RESOURCE_ALERT:cooldown:E1:cpu:total_used_pct"] = 1
        mock_redis_client.client.exists.side_effect = RedisTimeoutError("slow")
        assert await cooldown.is_cooling_down("E1", "cpu", "total_used_pct") is True


# ----------------------------------------------------------------------
# set_cooldown
# ----------------------------------------------------------------------
@pytest.mark.unit
class TestSetCooldown:
    async def test_set_cooldown_writes_to_redis_and_local(
        self, cooldown, mock_redis_client
    ):
        await cooldown.set_cooldown("E1", "cpu", "total_used_pct", cooldown_minutes=30)
        # Redis SETEX called with seconds
        mock_redis_client.client.setex.assert_awaited_once()
        args = mock_redis_client.client.setex.call_args.args
        assert args[0] == "RESOURCE_ALERT:cooldown:E1:cpu:total_used_pct"
        assert args[1] == 30 * 60  # seconds
        assert args[2] == "1"
        # Local cache also populated
        assert "RESOURCE_ALERT:cooldown:E1:cpu:total_used_pct" in cooldown._local

    async def test_set_cooldown_local_populated_even_if_redis_fails(
        self, cooldown, mock_redis_client
    ):
        """Local fallback must be written first so a Redis failure cannot skip it."""
        mock_redis_client.client.setex.side_effect = RedisConnectionError("boom")
        # Should NOT raise
        await cooldown.set_cooldown("E1", "cpu", "total_used_pct", cooldown_minutes=30)
        assert "RESOURCE_ALERT:cooldown:E1:cpu:total_used_pct" in cooldown._local


# ----------------------------------------------------------------------
# Batch + clear
# ----------------------------------------------------------------------
@pytest.mark.unit
class TestBatchAndClear:
    async def test_batch_pipeline_returns_dict(self, cooldown, mock_redis_client):
        # Mock the pipeline context manager behavior
        pipe = AsyncMock()
        pipe.execute.return_value = [1, 0, 1]
        pipe.exists = MagicMock()  # non-async in pipeline, queues commands

        pipeline_ctx = AsyncMock()
        pipeline_ctx.__aenter__.return_value = pipe
        pipeline_ctx.__aexit__.return_value = None
        mock_redis_client.client.pipeline = MagicMock(return_value=pipeline_ctx)

        checks = [
            ("E1", "cpu", "total_used_pct"),
            ("E2", "cpu", "total_used_pct"),
            ("E3", "mem", "total_used_pct"),
        ]
        result = await cooldown.is_cooling_down_batch(checks)
        assert result[("E1", "cpu", "total_used_pct")] is True
        assert result[("E2", "cpu", "total_used_pct")] is False
        assert result[("E3", "mem", "total_used_pct")] is True

    async def test_batch_redis_down_uses_local_fallback(
        self, cooldown, mock_redis_client
    ):
        mock_redis_client.client.pipeline = MagicMock(
            side_effect=RedisConnectionError("boom")
        )
        cooldown._local["RESOURCE_ALERT:cooldown:E1:cpu:total_used_pct"] = 1
        checks = [
            ("E1", "cpu", "total_used_pct"),
            ("E2", "cpu", "total_used_pct"),
        ]
        result = await cooldown.is_cooling_down_batch(checks)
        assert result[("E1", "cpu", "total_used_pct")] is True
        assert result[("E2", "cpu", "total_used_pct")] is False

    async def test_clear_cooldown_deletes_redis_and_local(
        self, cooldown, mock_redis_client
    ):
        cooldown._local["RESOURCE_ALERT:cooldown:E1:cpu:total_used_pct"] = 1
        await cooldown.clear_cooldown("E1", "cpu", "total_used_pct")
        mock_redis_client.client.delete.assert_awaited_once_with(
            "RESOURCE_ALERT:cooldown:E1:cpu:total_used_pct"
        )
        assert "RESOURCE_ALERT:cooldown:E1:cpu:total_used_pct" not in cooldown._local

    async def test_clear_cooldown_swallows_redis_errors(
        self, cooldown, mock_redis_client
    ):
        cooldown._local["RESOURCE_ALERT:cooldown:E1:cpu:total_used_pct"] = 1
        mock_redis_client.client.delete.side_effect = RedisConnectionError("boom")
        # Must not raise — local is still cleared
        await cooldown.clear_cooldown("E1", "cpu", "total_used_pct")
        assert "RESOURCE_ALERT:cooldown:E1:cpu:total_used_pct" not in cooldown._local


# ----------------------------------------------------------------------
# Debug Read-Only mode — writes must never hit Redis
# ----------------------------------------------------------------------
@pytest.mark.unit
class TestDebugReadOnlyGuard:
    """★ Debug Read-Only: set/clear must not touch Redis. The local
    TTLCache is still updated so that within a single debug run
    ``is_cooling_down`` still behaves correctly (preventing
    the debug scheduler from emitting the same "would-alert" log
    over and over for the same metric)."""

    @pytest.fixture
    def debug_cooldown(self, mock_redis_client) -> AlertCooldownManager:
        debug_settings = AppSettings(debug_read_only=True)
        return AlertCooldownManager(
            mock_redis_client, settings=debug_settings
        )

    async def test_set_cooldown_skips_redis_in_debug_mode(
        self, debug_cooldown, mock_redis_client
    ):
        await debug_cooldown.set_cooldown("E1", "cpu", "total_used_pct", 10)
        # Redis write suppressed
        mock_redis_client.client.setex.assert_not_called()
        # But local cache still populated so subsequent checks work
        assert (
            "RESOURCE_ALERT:cooldown:E1:cpu:total_used_pct"
            in debug_cooldown._local
        )

    async def test_clear_cooldown_skips_redis_in_debug_mode(
        self, debug_cooldown, mock_redis_client
    ):
        debug_cooldown._local[
            "RESOURCE_ALERT:cooldown:E1:cpu:total_used_pct"
        ] = 1
        await debug_cooldown.clear_cooldown("E1", "cpu", "total_used_pct")
        # Redis delete suppressed
        mock_redis_client.client.delete.assert_not_called()
        # Local still cleared
        assert (
            "RESOURCE_ALERT:cooldown:E1:cpu:total_used_pct"
            not in debug_cooldown._local
        )

    async def test_is_cooling_down_reads_local_in_debug_mode(
        self, debug_cooldown, mock_redis_client
    ):
        """Read-path in debug mode: go straight to local cache, skip Redis.
        Reading from prod Redis would be safe but misleading — the point
        of debug mode is a self-contained single-run view."""
        debug_cooldown._local[
            "RESOURCE_ALERT:cooldown:E1:cpu:total_used_pct"
        ] = 1
        assert (
            await debug_cooldown.is_cooling_down("E1", "cpu", "total_used_pct")
            is True
        )
        mock_redis_client.client.exists.assert_not_called()

    async def test_is_cooling_down_batch_uses_local_in_debug_mode(
        self, debug_cooldown, mock_redis_client
    ):
        debug_cooldown._local[
            "RESOURCE_ALERT:cooldown:E1:cpu:total_used_pct"
        ] = 1
        result = await debug_cooldown.is_cooling_down_batch(
            [
                ("E1", "cpu", "total_used_pct"),
                ("E2", "mem", "total_used_pct"),
            ]
        )
        assert result == {
            ("E1", "cpu", "total_used_pct"): True,
            ("E2", "mem", "total_used_pct"): False,
        }
        # pipeline never invoked
        mock_redis_client.client.pipeline.assert_not_called()

    async def test_normal_mode_still_writes_to_redis(
        self, cooldown, mock_redis_client
    ):
        """Safety net: the default (no debug settings) path still writes."""
        await cooldown.set_cooldown("E1", "cpu", "total_used_pct", 10)
        mock_redis_client.client.setex.assert_awaited_once()
