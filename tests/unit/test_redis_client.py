"""Tests for src.cache.redis_client (Redis 5.0.6 compat)."""
import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from src.config.settings import AppSettings
from src.cache.redis_client import RedisClient


@pytest.fixture
def settings() -> AppSettings:
    return AppSettings(
        redis_url="redis://redis:6379/0",
        redis_password="secretpass",
        redis_key_prefix="RESOURCE_ALERT",
    )


@pytest.mark.unit
class TestRedisClientConnect:
    async def test_connect_passes_protocol_2_for_redis_5x(self, settings):
        """Redis 5.0.6 does not support RESP3 → must pin protocol=2."""
        client = RedisClient(settings)
        with patch("src.cache.redis_client.Redis") as mock_cls:
            mock_instance = AsyncMock()
            mock_cls.from_url.return_value = mock_instance
            await client.connect()
        kwargs = mock_cls.from_url.call_args.kwargs
        assert kwargs["protocol"] == 2

    async def test_connect_passes_simple_auth_password(self, settings):
        """Redis 5.x has no ACL — only simple `requirepass` AUTH."""
        client = RedisClient(settings)
        with patch("src.cache.redis_client.Redis") as mock_cls:
            mock_instance = AsyncMock()
            mock_cls.from_url.return_value = mock_instance
            await client.connect()
        kwargs = mock_cls.from_url.call_args.kwargs
        assert kwargs["password"] == "secretpass"

    async def test_connect_omits_password_when_empty(self):
        settings = AppSettings(redis_url="redis://redis:6379/0", redis_password="")
        client = RedisClient(settings)
        with patch("src.cache.redis_client.Redis") as mock_cls:
            mock_instance = AsyncMock()
            mock_cls.from_url.return_value = mock_instance
            await client.connect()
        kwargs = mock_cls.from_url.call_args.kwargs
        assert kwargs.get("password") is None

    async def test_connect_uses_decode_responses(self, settings):
        client = RedisClient(settings)
        with patch("src.cache.redis_client.Redis") as mock_cls:
            mock_instance = AsyncMock()
            mock_cls.from_url.return_value = mock_instance
            await client.connect()
        kwargs = mock_cls.from_url.call_args.kwargs
        assert kwargs["decode_responses"] is True

    async def test_connect_calls_ping(self, settings):
        client = RedisClient(settings)
        with patch("src.cache.redis_client.Redis") as mock_cls:
            mock_instance = AsyncMock()
            mock_cls.from_url.return_value = mock_instance
            await client.connect()
        mock_instance.ping.assert_awaited_once()


@pytest.mark.unit
class TestRedisConnectWithRetry:
    """v6 P0-2: Redis startup retry, matching MongoClient.connect_with_retry.

    Without retry, a Redis pod that boots a few hundred ms after RMS will
    cause CrashLoopBackoff because the single-attempt connect raises
    immediately. The plan: 3 attempts × linear backoff (1s, 2s) = ~3s of
    sleeps + ping latencies. Worst case ~10s, well under the 45s ZK budget.
    """

    async def test_connect_with_retry_succeeds_on_first_attempt(self, settings):
        client = RedisClient(settings)
        with patch("src.cache.redis_client.Redis") as mock_cls:
            instance = AsyncMock()
            mock_cls.from_url.return_value = instance
            await client.connect_with_retry()
        instance.ping.assert_awaited_once()

    async def test_connect_with_retry_succeeds_on_second_attempt(self, settings):
        """First ping fails, second succeeds — must retry, not raise."""
        client = RedisClient(settings)
        attempts = {"n": 0}

        async def ping_side_effect():
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise ConnectionError("transient")
            return True

        with patch("src.cache.redis_client.Redis") as mock_cls:
            instance = AsyncMock()
            instance.ping.side_effect = ping_side_effect
            mock_cls.from_url.return_value = instance
            with patch("src.cache.redis_client.asyncio.sleep", new=AsyncMock()):
                await client.connect_with_retry(max_attempts=3, backoff=1.0)
        assert attempts["n"] == 2

    async def test_connect_with_retry_exhausts_after_max_attempts(self, settings):
        client = RedisClient(settings)
        with patch("src.cache.redis_client.Redis") as mock_cls:
            instance = AsyncMock()
            instance.ping.side_effect = ConnectionError("never up")
            mock_cls.from_url.return_value = instance
            with patch("src.cache.redis_client.asyncio.sleep", new=AsyncMock()):
                with pytest.raises(ConnectionError, match="never up"):
                    await client.connect_with_retry(max_attempts=3, backoff=1.0)
        assert instance.ping.await_count == 3

    async def test_connect_with_retry_default_attempts_is_3(self, settings):
        client = RedisClient(settings)
        with patch("src.cache.redis_client.Redis") as mock_cls:
            instance = AsyncMock()
            instance.ping.side_effect = ConnectionError("boom")
            mock_cls.from_url.return_value = instance
            with patch("src.cache.redis_client.asyncio.sleep", new=AsyncMock()):
                with pytest.raises(ConnectionError):
                    await client.connect_with_retry()
        assert instance.ping.await_count == 3

    async def test_connect_with_retry_linear_backoff_pattern(self, settings):
        """Sleeps must be linear: backoff * attempt (1s, 2s for 3 attempts)."""
        client = RedisClient(settings)
        sleep_delays: list[float] = []

        async def fake_sleep(delay):
            sleep_delays.append(delay)

        with patch("src.cache.redis_client.Redis") as mock_cls:
            instance = AsyncMock()
            instance.ping.side_effect = ConnectionError("boom")
            mock_cls.from_url.return_value = instance
            with patch(
                "src.cache.redis_client.asyncio.sleep", side_effect=fake_sleep
            ):
                with pytest.raises(ConnectionError):
                    await client.connect_with_retry(max_attempts=3, backoff=1.0)
        # 3 attempts → 2 sleeps between them: 1×1=1, 1×2=2
        assert sleep_delays == [1.0, 2.0]

    async def test_connect_with_retry_does_not_leave_half_initialized_client(
        self, settings
    ):
        """Failed connect must NOT leave self._client set to a broken object."""
        client = RedisClient(settings)
        with patch("src.cache.redis_client.Redis") as mock_cls:
            instance = AsyncMock()
            instance.ping.side_effect = ConnectionError("boom")
            mock_cls.from_url.return_value = instance
            with patch("src.cache.redis_client.asyncio.sleep", new=AsyncMock()):
                with pytest.raises(ConnectionError):
                    await client.connect_with_retry(max_attempts=2, backoff=0.0)
        assert client._client is None


@pytest.mark.unit
class TestRedisClientKeyPrefix:
    def test_key_prefix_exposed(self, settings):
        client = RedisClient(settings)
        assert client.key_prefix == "RESOURCE_ALERT"


@pytest.mark.unit
class TestRedisClientPing:
    async def test_ping_returns_true_on_success(self, settings):
        client = RedisClient(settings)
        client._client = AsyncMock()
        client._client.ping.return_value = True
        assert await client.ping() is True

    async def test_ping_returns_false_on_exception(self, settings):
        client = RedisClient(settings)
        client._client = AsyncMock()
        client._client.ping.side_effect = ConnectionError("boom")
        assert await client.ping() is False

    async def test_ping_returns_false_when_not_connected(self, settings):
        client = RedisClient(settings)
        assert await client.ping() is False


@pytest.mark.unit
class TestRedisClientClose:
    async def test_close_calls_underlying(self, settings):
        client = RedisClient(settings)
        underlying = AsyncMock()
        client._client = underlying
        await client.close()
        underlying.close.assert_awaited_once()
        assert client._client is None
