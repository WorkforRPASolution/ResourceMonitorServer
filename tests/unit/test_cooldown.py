"""Tests for src.cache.cooldown (AlertCooldownManager) — v2 5-dim key.

Key format: ``{prefix}:cooldown:{process}:{eqp}:{proc}:{notify}:{severity}``.
"""
from unittest.mock import AsyncMock, MagicMock

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import TimeoutError as RedisTimeoutError

from src.cache.cooldown import AlertCooldownManager
from src.config.settings import AppSettings

pytestmark = pytest.mark.unit

# canonical v2 args + the key they produce
_ARGS = ("CVD", "E1", "@system", "default", "WARNING")
_KEY = "RESOURCE_ALERT:cooldown:CVD:E1:@system:default:WARNING"


@pytest.fixture
def mock_redis_client() -> MagicMock:
    rc = MagicMock()
    rc.key_prefix = "RESOURCE_ALERT"
    rc.client = AsyncMock()
    return rc


@pytest.fixture
def cooldown(mock_redis_client) -> AlertCooldownManager:
    return AlertCooldownManager(mock_redis_client)


class TestIsCoolingDown:
    async def test_true_when_redis_key_exists(self, cooldown, mock_redis_client):
        mock_redis_client.client.exists.return_value = 1
        assert await cooldown.is_cooling_down(*_ARGS) is True

    async def test_false_when_redis_key_missing(self, cooldown, mock_redis_client):
        mock_redis_client.client.exists.return_value = 0
        assert await cooldown.is_cooling_down(*_ARGS) is False

    async def test_key_format_is_5_dim(self, cooldown, mock_redis_client):
        mock_redis_client.client.exists.return_value = 0
        await cooldown.is_cooling_down(*_ARGS)
        assert mock_redis_client.client.exists.call_args.args[0] == _KEY

    async def test_severity_separates_keys(self, cooldown, mock_redis_client):
        mock_redis_client.client.exists.return_value = 0
        await cooldown.is_cooling_down("CVD", "E1", "@system", "default", "CRITICAL")
        key = mock_redis_client.client.exists.call_args.args[0]
        assert key.endswith(":CRITICAL")

    async def test_redis_down_falls_back_to_local(self, cooldown, mock_redis_client):
        cooldown._local[_KEY] = 1
        mock_redis_client.client.exists.side_effect = RedisConnectionError("boom")
        assert await cooldown.is_cooling_down(*_ARGS) is True

    async def test_redis_down_no_local_returns_false(self, cooldown, mock_redis_client):
        mock_redis_client.client.exists.side_effect = RedisConnectionError("boom")
        assert await cooldown.is_cooling_down(*_ARGS) is False

    async def test_redis_timeout_falls_back_to_local(self, cooldown, mock_redis_client):
        cooldown._local[_KEY] = 1
        mock_redis_client.client.exists.side_effect = RedisTimeoutError("slow")
        assert await cooldown.is_cooling_down(*_ARGS) is True


class TestSetCooldown:
    async def test_writes_redis_and_local(self, cooldown, mock_redis_client):
        await cooldown.set_cooldown(*_ARGS, cooldown_minutes=30)
        mock_redis_client.client.setex.assert_awaited_once()
        args = mock_redis_client.client.setex.call_args.args
        assert args[0] == _KEY
        assert args[1] == 30 * 60
        assert args[2] == "1"
        assert _KEY in cooldown._local

    async def test_local_populated_even_if_redis_fails(self, cooldown, mock_redis_client):
        mock_redis_client.client.setex.side_effect = RedisConnectionError("boom")
        await cooldown.set_cooldown(*_ARGS, cooldown_minutes=30)
        assert _KEY in cooldown._local


class TestBatchAndClear:
    async def test_batch_pipeline_returns_dict(self, cooldown, mock_redis_client):
        pipe = AsyncMock()
        pipe.execute.return_value = [1, 0, 1]
        pipe.exists = MagicMock()
        pipeline_ctx = AsyncMock()
        pipeline_ctx.__aenter__.return_value = pipe
        pipeline_ctx.__aexit__.return_value = None
        mock_redis_client.client.pipeline = MagicMock(return_value=pipeline_ctx)

        checks = [
            ("CVD", "E1", "@system", "default", "WARNING"),
            ("CVD", "E2", "@system", "default", "WARNING"),
            ("CVD", "E3", "@system", "default", "CRITICAL"),
        ]
        result = await cooldown.is_cooling_down_batch(checks)
        assert result[checks[0]] is True
        assert result[checks[1]] is False
        assert result[checks[2]] is True

    async def test_batch_redis_down_uses_local(self, cooldown, mock_redis_client):
        mock_redis_client.client.pipeline = MagicMock(
            side_effect=RedisConnectionError("boom")
        )
        cooldown._local[_KEY] = 1
        checks = [_ARGS, ("CVD", "E2", "@system", "default", "WARNING")]
        result = await cooldown.is_cooling_down_batch(checks)
        assert result[_ARGS] is True
        assert result[checks[1]] is False

    async def test_clear_deletes_redis_and_local(self, cooldown, mock_redis_client):
        cooldown._local[_KEY] = 1
        await cooldown.clear_cooldown(*_ARGS)
        mock_redis_client.client.delete.assert_awaited_once_with(_KEY)
        assert _KEY not in cooldown._local

    async def test_clear_swallows_redis_errors(self, cooldown, mock_redis_client):
        cooldown._local[_KEY] = 1
        mock_redis_client.client.delete.side_effect = RedisConnectionError("boom")
        await cooldown.clear_cooldown(*_ARGS)
        assert _KEY not in cooldown._local


class TestDebugReadOnlyGuard:
    @pytest.fixture
    def debug_cooldown(self, mock_redis_client) -> AlertCooldownManager:
        return AlertCooldownManager(mock_redis_client, settings=AppSettings(debug_read_only=True))

    async def test_set_skips_redis(self, debug_cooldown, mock_redis_client):
        await debug_cooldown.set_cooldown(*_ARGS, cooldown_minutes=10)
        mock_redis_client.client.setex.assert_not_called()
        assert _KEY in debug_cooldown._local

    async def test_clear_skips_redis(self, debug_cooldown, mock_redis_client):
        debug_cooldown._local[_KEY] = 1
        await debug_cooldown.clear_cooldown(*_ARGS)
        mock_redis_client.client.delete.assert_not_called()
        assert _KEY not in debug_cooldown._local

    async def test_is_cooling_down_reads_local(self, debug_cooldown, mock_redis_client):
        debug_cooldown._local[_KEY] = 1
        assert await debug_cooldown.is_cooling_down(*_ARGS) is True
        mock_redis_client.client.exists.assert_not_called()

    async def test_batch_uses_local(self, debug_cooldown, mock_redis_client):
        debug_cooldown._local[_KEY] = 1
        checks = [_ARGS, ("CVD", "E2", "@system", "default", "WARNING")]
        result = await debug_cooldown.is_cooling_down_batch(checks)
        assert result == {_ARGS: True, checks[1]: False}
        mock_redis_client.client.pipeline.assert_not_called()

    async def test_normal_mode_still_writes(self, cooldown, mock_redis_client):
        await cooldown.set_cooldown(*_ARGS, cooldown_minutes=10)
        mock_redis_client.client.setex.assert_awaited_once()
