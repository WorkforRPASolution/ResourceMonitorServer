"""Cooldown degraded mode — Redis 다운 시 local fallback으로 이메일 폭주 방지.

★★ 핵심 시나리오 ★★

Phase 0 v4 이전에는 Redis가 죽으면 `is_cooling_down`이 무조건 False를 반환
하여 매 분석 cycle마다 동일 알림이 다시 발송됐다. v4는 local TTLCache
fallback으로 이 폭주를 막는다. 본 파일은 **실제로 `ars-redis` 컨테이너를
중단시켜** 그 동작을 검증한다.

주의:
  - 다른 테스트와 순서 의존이 생기면 안 되므로, 시작/끝에 반드시 Redis를
    재기동한다.
  - `docker start ars-redis` 후 실제 ready까지 수 초 소요 → polling.
  - 회귀 가드: 혹시 누군가 cooldown 경로에서 local fallback을 제거하면
    이 테스트가 즉시 실패한다.
"""
from __future__ import annotations

import asyncio
import subprocess

import pytest
from pydantic import SecretStr

from src.cache.cooldown import AlertCooldownManager
from src.cache.redis_client import RedisClient
from src.config.settings import AppSettings

pytestmark = [pytest.mark.integration, pytest.mark.slow]

REDIS_CONTAINER = "ars-redis"


# ----------------------------------------------------------------------
# Docker control helpers (host shell)
# ----------------------------------------------------------------------
def _docker(*args: str) -> None:
    subprocess.run(["docker", *args], check=True, capture_output=True)


async def _wait_redis_ready(url: str, timeout: float = 15.0) -> None:
    """Redis가 PING에 응답할 때까지 poll. Timeout이면 RuntimeError."""
    from redis.asyncio import Redis

    deadline = asyncio.get_event_loop().time() + timeout
    last_err: Exception | None = None
    while asyncio.get_event_loop().time() < deadline:
        try:
            r = Redis.from_url(url, decode_responses=True, protocol=2)
            try:
                if await r.ping():
                    return
            finally:
                await r.aclose()
        except Exception as e:
            last_err = e
        await asyncio.sleep(0.3)
    raise RuntimeError(f"Redis not ready within {timeout}s: {last_err!r}")


@pytest.fixture
async def redis_lifecycle():
    """시나리오 시작/끝에 반드시 Redis가 살아있도록 보장."""
    await _wait_redis_ready("redis://localhost:6379/15")
    yield
    # Teardown: 어떤 상태든 컨테이너를 다시 띄우고 ready 대기
    try:
        _docker("start", REDIS_CONTAINER)
    except subprocess.CalledProcessError:
        pass
    await _wait_redis_ready("redis://localhost:6379/15")


async def _make_cooldown_mgr(ns, key_prefix: str) -> tuple[RedisClient, AlertCooldownManager]:
    settings = AppSettings(
        redis_url="redis://localhost:6379/15",
        redis_password=SecretStr(""),
        redis_key_prefix=key_prefix,
    )
    client = RedisClient(settings)
    await client.connect()
    return client, AlertCooldownManager(client)


# ----------------------------------------------------------------------
# Scenario 1: 정상 → Redis stop → degraded → set/is_cooling_down 모두 동작
# ----------------------------------------------------------------------
async def test_cooldown_degraded_mode_prevents_flood(ns, redis_lifecycle):
    """Redis 다운 중에 set_cooldown + is_cooling_down이 local fallback으로
    동작해야 이메일 폭주가 막힌다."""
    prefix = f"{ns.redis_prefix}_degraded1"
    client, mgr = await _make_cooldown_mgr(ns, prefix)
    try:
        eqp, cat, met = "EQP_DEG", "cpu", "usage"
        # 정상 모드 sanity
        assert await mgr.is_cooling_down(eqp, cat, met) is False

        # Redis 중단
        _docker("stop", REDIS_CONTAINER)
        try:
            # 1. set_cooldown은 Redis 실패해도 local에 기록되어야 함
            await mgr.set_cooldown(eqp, cat, met, cooldown_minutes=5)
            # 2. is_cooling_down은 local fallback으로 True 반환
            assert await mgr.is_cooling_down(eqp, cat, met) is True
            # 3. 다른 키는 여전히 False (local에 없음)
            assert await mgr.is_cooling_down(eqp, cat, "memory") is False
            # 4. 이메일 폭주 방지: 10번 반복해도 모두 True
            for _ in range(10):
                assert await mgr.is_cooling_down(eqp, cat, met) is True
        finally:
            _docker("start", REDIS_CONTAINER)
            await _wait_redis_ready("redis://localhost:6379/15")
    finally:
        await client.close()


# ----------------------------------------------------------------------
# Scenario 2: Redis 복구 후 새 set은 Redis로 가고, 옛 local 엔트리는 유지
# ----------------------------------------------------------------------
async def test_cooldown_post_recovery(ns, redis_lifecycle):
    """Redis 복구 이후 새 set_cooldown은 실제 Redis에 저장되어야 한다.
    (정확성 > 청소: 복구 중 만들어진 local 엔트리는 TTL로 자연 소멸)"""
    prefix = f"{ns.redis_prefix}_degraded2"
    client, mgr = await _make_cooldown_mgr(ns, prefix)
    try:
        # 다운 상태에서 local에 기록
        _docker("stop", REDIS_CONTAINER)
        await mgr.set_cooldown("EQP_R1", "cpu", "usage", cooldown_minutes=5)
        assert await mgr.is_cooling_down("EQP_R1", "cpu", "usage") is True

        # 복구
        _docker("start", REDIS_CONTAINER)
        await _wait_redis_ready("redis://localhost:6379/15")

        # 새 set_cooldown은 Redis로 감
        await mgr.set_cooldown("EQP_R2", "mem", "free", cooldown_minutes=5)
        assert await mgr.is_cooling_down("EQP_R2", "mem", "free") is True

        # Redis에 직접 쿼리해 키 존재 확인 (local이 아닌 Redis에서)
        # cooldown key format: {prefix}:cooldown:{eqp}:{cat}:{met}
        raw_key = f"{prefix}:cooldown:EQP_R2:mem:free"
        assert await client.client.exists(raw_key) > 0
    finally:
        await client.close()


# ----------------------------------------------------------------------
# Scenario 3: batch API도 degraded mode에서 local로 fallback
# ----------------------------------------------------------------------
async def test_cooldown_batch_degraded(ns, redis_lifecycle):
    prefix = f"{ns.redis_prefix}_degraded3"
    client, mgr = await _make_cooldown_mgr(ns, prefix)
    try:
        # 정상 상태에서 하나를 local+Redis에 기록, 다른 하나는 기록 안 함
        await mgr.set_cooldown("EQP_B1", "cpu", "usage", cooldown_minutes=5)

        _docker("stop", REDIS_CONTAINER)
        try:
            # degraded 상태에서 또 하나 추가 (local only)
            await mgr.set_cooldown("EQP_B2", "mem", "free", cooldown_minutes=5)

            # batch check — 두 개 True, 하나 False
            checks = [
                ("EQP_B1", "cpu", "usage"),
                ("EQP_B2", "mem", "free"),
                ("EQP_B3", "disk", "io"),
            ]
            result = await mgr.is_cooling_down_batch(checks)
            assert result[("EQP_B1", "cpu", "usage")] is True
            assert result[("EQP_B2", "mem", "free")] is True
            assert result[("EQP_B3", "disk", "io")] is False
        finally:
            _docker("start", REDIS_CONTAINER)
            await _wait_redis_ready("redis://localhost:6379/15")
    finally:
        await client.close()
