"""Tests for src.cache.redis_client (Redis 5.0.6 compat)."""
from unittest.mock import AsyncMock, patch

import pytest

from src.cache.redis_client import RedisClient
from src.config.settings import AppSettings


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
            ), pytest.raises(ConnectionError):
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
        # redis-py 5.0.1+ deprecates close() → must use aclose()
        underlying.aclose.assert_awaited_once()
        assert client._client is None


@pytest.mark.unit
class TestRedisSentinelSettings:
    """redis_sentinels 는 콤마/JSON 문자열을 list[str] 로 파싱(es_hosts 패턴)."""

    def test_sentinels_parsed_from_comma_string(self):
        s = AppSettings(redis_sentinels="h0:26379,h1:26379,h2:26379")
        assert s.redis_sentinels == ["h0:26379", "h1:26379", "h2:26379"]

    def test_sentinels_empty_by_default(self):
        assert AppSettings().redis_sentinels == []

    def test_sentinel_master_default_is_mymaster(self):
        assert AppSettings().redis_sentinel_master == "mymaster"


@pytest.mark.unit
class TestRedisSentinelConnect:
    """redis_sentinels 가 설정되면 Sentinel.master_for 로 현재 마스터에 연결한다.

    기존 실 사내 설정 형태:
      mdb-redis-ha-announce-0..2.<ns>.svc.cluster.local:26379  + master 그룹 'mymaster'
    """

    @pytest.fixture
    def sentinel_settings(self) -> AppSettings:
        return AppSettings(
            redis_sentinels="h0:26379,h1:26379,h2:26379",
            redis_sentinel_master="mymaster",
            redis_db=5,
            redis_password="secretpass",
        )

    async def test_sentinel_mode_uses_master_for_not_from_url(self, sentinel_settings):
        client = RedisClient(sentinel_settings)
        with patch("src.cache.redis_client.Sentinel") as mock_sentinel_cls, patch(
            "src.cache.redis_client.Redis"
        ) as mock_redis_cls:
            sentinel_obj = mock_sentinel_cls.return_value
            master = AsyncMock()
            sentinel_obj.master_for.return_value = master
            await client.connect()
        mock_redis_cls.from_url.assert_not_called()
        mock_sentinel_cls.assert_called_once()
        master.ping.assert_awaited_once()
        assert client._client is master

    async def test_sentinel_hosts_parsed_to_host_port_tuples(self, sentinel_settings):
        client = RedisClient(sentinel_settings)
        with patch("src.cache.redis_client.Sentinel") as mock_sentinel_cls:
            mock_sentinel_cls.return_value.master_for.return_value = AsyncMock()
            await client.connect()
        sentinels_arg = mock_sentinel_cls.call_args.args[0]
        assert sentinels_arg == [("h0", 26379), ("h1", 26379), ("h2", 26379)]

    async def test_master_name_and_db_passed_to_master_for(self, sentinel_settings):
        client = RedisClient(sentinel_settings)
        with patch("src.cache.redis_client.Sentinel") as mock_sentinel_cls:
            mock_sentinel_cls.return_value.master_for.return_value = AsyncMock()
            await client.connect()
        args, kwargs = mock_sentinel_cls.return_value.master_for.call_args
        assert args[0] == "mymaster"
        assert kwargs["db"] == 5

    async def test_sentinel_pins_protocol_2_and_data_password(self, sentinel_settings):
        client = RedisClient(sentinel_settings)
        with patch("src.cache.redis_client.Sentinel") as mock_sentinel_cls:
            mock_sentinel_cls.return_value.master_for.return_value = AsyncMock()
            await client.connect()
        kwargs = mock_sentinel_cls.call_args.kwargs
        assert kwargs["protocol"] == 2
        assert kwargs["password"] == "secretpass"

    async def test_sentinel_auth_omitted_when_no_sentinel_password(
        self, sentinel_settings
    ):
        """redis_sentinel_password 미설정 → 센티널엔 AUTH 미전송(데이터 비번으로 폴백 X).

        redis-ha 의 sentinel.auth=false 처럼 데이터 노드는 requirepass 가 있어도
        센티널엔 비번이 없는 구성을 지원하기 위함. 데이터 비번으로 폴백하면
        센티널이 'Client sent AUTH, but no password is set' 로 거부한다.
        """
        client = RedisClient(sentinel_settings)
        with patch("src.cache.redis_client.Sentinel") as mock_sentinel_cls:
            mock_sentinel_cls.return_value.master_for.return_value = AsyncMock()
            await client.connect()
        kwargs = mock_sentinel_cls.call_args.kwargs
        assert kwargs["sentinel_kwargs"] is None       # 센티널 AUTH 없음
        assert kwargs["password"] == "secretpass"      # 데이터 노드는 여전히 AUTH

    async def test_separate_sentinel_password_used_when_set(self):
        settings = AppSettings(
            redis_sentinels="h0:26379",
            redis_password="datapass",
            redis_sentinel_password="sentpass",
        )
        client = RedisClient(settings)
        with patch("src.cache.redis_client.Sentinel") as mock_sentinel_cls:
            mock_sentinel_cls.return_value.master_for.return_value = AsyncMock()
            await client.connect()
        kwargs = mock_sentinel_cls.call_args.kwargs
        assert kwargs["sentinel_kwargs"] == {"password": "sentpass"}
        assert kwargs["password"] == "datapass"  # 데이터 노드는 redis_password

    async def test_host_without_port_defaults_to_26379(self):
        settings = AppSettings(redis_sentinels="h0,h1:26380")
        client = RedisClient(settings)
        with patch("src.cache.redis_client.Sentinel") as mock_sentinel_cls:
            mock_sentinel_cls.return_value.master_for.return_value = AsyncMock()
            await client.connect()
        assert mock_sentinel_cls.call_args.args[0] == [("h0", 26379), ("h1", 26380)]

    async def test_url_mode_when_no_sentinels(self, settings):
        """sentinels 미설정 → 기존 단일 URL(from_url) 동작 유지."""
        client = RedisClient(settings)
        with patch("src.cache.redis_client.Redis") as mock_cls, patch(
            "src.cache.redis_client.Sentinel"
        ) as mock_sentinel_cls:
            mock_cls.from_url.return_value = AsyncMock()
            await client.connect()
        mock_cls.from_url.assert_called_once()
        mock_sentinel_cls.assert_not_called()

    async def test_close_also_closes_sentinel_monitor_connections(
        self, sentinel_settings
    ):
        """close() 는 master pool 뿐 아니라 Sentinel 모니터링 연결도 정리한다(누수 방지)."""
        client = RedisClient(sentinel_settings)
        s0, s1 = AsyncMock(), AsyncMock()
        with patch("src.cache.redis_client.Sentinel") as mock_sentinel_cls:
            sentinel_obj = mock_sentinel_cls.return_value
            sentinel_obj.sentinels = [s0, s1]
            sentinel_obj.master_for.return_value = AsyncMock()
            await client.connect()
            await client.close()
        s0.aclose.assert_awaited_once()
        s1.aclose.assert_awaited_once()
        assert client._client is None
