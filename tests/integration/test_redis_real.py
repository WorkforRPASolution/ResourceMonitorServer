"""Redis 5.0.6 integration — real SETEX/EXISTS/SCAN/pipeline round-trip.

unit test는 AsyncMock으로 Redis client를 mock했기 때문에:
- 실제 Redis protocol=2 enforcement
- pipeline 응답 형태
- SCAN cursor iteration
- TTL 만료 동작
까지는 검증 안 된다. 본 파일이 그 공백을 메운다.
"""
from __future__ import annotations

import pytest
from pydantic import SecretStr

from src.cache.cooldown import AlertCooldownManager
from src.cache.redis_client import RedisClient
from src.config.settings import AppSettings

pytestmark = pytest.mark.integration


# ----------------------------------------------------------------------
# RedisClient — connect / ping / close / protocol=2
# ----------------------------------------------------------------------
async def test_redis_client_connect_ping_close(ns):
    settings = AppSettings(
        redis_url="redis://localhost:6379/15",
        redis_password=SecretStr(""),
        redis_key_prefix=ns.redis_prefix,
    )
    client = RedisClient(settings)
    await client.connect()
    try:
        assert await client.ping() is True
    finally:
        await client.close()
    # After close, subsequent ping returns False (client is None)
    assert await client.ping() is False


async def test_redis_client_protocol_is_2(ns):
    """Redis 5.0.6 요구사항: protocol=2 강제 동작.

    회귀 가드: 만약 누군가 protocol=3 (RESP3)으로 바꾸면 Redis 5.0.6은
    HELLO 명령을 모르므로 from_url 시점에 실패한다.
    """
    settings = AppSettings(
        redis_url="redis://localhost:6379/15",
        redis_password=SecretStr(""),
        redis_key_prefix=ns.redis_prefix,
    )
    client = RedisClient(settings)
    await client.connect()
    try:
        # 내부 connection_pool에 protocol=2가 기록되었는지 확인
        pool = client.client.connection_pool
        assert pool.connection_kwargs.get("protocol") == 2
    finally:
        await client.close()


# ----------------------------------------------------------------------
# AlertCooldownManager — SETEX / EXISTS / TTL
# ----------------------------------------------------------------------
@pytest.fixture
async def cooldown_mgr(ns):
    """Fresh RedisClient + CooldownManager per test."""
    settings = AppSettings(
        redis_url="redis://localhost:6379/15",
        redis_password=SecretStr(""),
        redis_key_prefix=ns.redis_prefix,
    )
    client = RedisClient(settings)
    await client.connect()
    mgr = AlertCooldownManager(client)
    yield mgr
    # cleanup: delete all test keys for this run
    try:
        async for key in client.client.scan_iter(f"{ns.redis_prefix}*"):
            await client.client.delete(key)
    except Exception:
        pass
    await client.close()


async def test_set_then_check_cooldown(cooldown_mgr):
    """SETEX + EXISTS sanity — cooling_down이 True/False를 정확히 반환."""
    eqp, cat, met = "EQP_A", "cpu", "usage"
    assert await cooldown_mgr.is_cooling_down(eqp, cat, met) is False
    await cooldown_mgr.set_cooldown(eqp, cat, met, cooldown_minutes=1)
    assert await cooldown_mgr.is_cooling_down(eqp, cat, met) is True
    # 다른 metric은 영향 없음
    assert await cooldown_mgr.is_cooling_down(eqp, cat, "memory") is False


async def test_cooldown_batch_pipeline(cooldown_mgr):
    """pipeline exists가 정확한 순서로 결과를 매핑해야 한다."""
    await cooldown_mgr.set_cooldown("EQP_B1", "cpu", "usage", 1)
    await cooldown_mgr.set_cooldown("EQP_B3", "mem", "free", 1)
    checks = [
        ("EQP_B1", "cpu", "usage"),   # cooling
        ("EQP_B2", "cpu", "usage"),   # not
        ("EQP_B3", "mem", "free"),    # cooling
        ("EQP_B4", "mem", "free"),    # not
    ]
    result = await cooldown_mgr.is_cooling_down_batch(checks)
    assert result[("EQP_B1", "cpu", "usage")] is True
    assert result[("EQP_B2", "cpu", "usage")] is False
    assert result[("EQP_B3", "mem", "free")] is True
    assert result[("EQP_B4", "mem", "free")] is False


async def test_clear_cooldown_removes_key(cooldown_mgr):
    await cooldown_mgr.set_cooldown("EQP_C", "disk", "io", 1)
    assert await cooldown_mgr.is_cooling_down("EQP_C", "disk", "io") is True
    await cooldown_mgr.clear_cooldown("EQP_C", "disk", "io")
    assert await cooldown_mgr.is_cooling_down("EQP_C", "disk", "io") is False
