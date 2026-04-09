"""Alert cooldown manager with Redis + local fallback.

Design (v4):
    Primary storage is Redis (SETEX/EXISTS only — no Redis 6+ commands).
    When Redis is unavailable we fall back to a local bounded `TTLCache` so
    that a Redis outage cannot turn into an email flood. Without the local
    cache a "degraded → False" path would mean *every* cycle re-sends the
    same alert for every triggered equipment.

Write path:
    ``set_cooldown`` always writes to the local cache first, then attempts
    Redis. A Redis failure is logged but does not prevent the local entry,
    so subsequent `is_cooling_down` checks still see the cooldown.

Read path:
    ``is_cooling_down`` prefers Redis. On Redis failure it consults the local
    cache — returning True if a local entry exists, False otherwise. The
    False case (first-ever alert during an outage) is intentional so the
    very first notification still gets through.
"""
from __future__ import annotations

import structlog
from cachetools import TTLCache
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import TimeoutError as RedisTimeoutError

from src.cache.redis_client import RedisClient
from src.config.constants import (
    COOLDOWN_LOCAL_CACHE_MAX_SIZE,
    COOLDOWN_LOCAL_CACHE_MAX_TTL_SEC,
)
from src.config.settings import AppSettings

logger = structlog.get_logger(__name__)

# Exceptions that signal "Redis is unavailable — fall back to local."
_REDIS_UNAVAILABLE = (RedisConnectionError, RedisTimeoutError)


class AlertCooldownManager:
    def __init__(
        self,
        redis_client: RedisClient,
        settings: AppSettings | None = None,
    ) -> None:
        self._redis = redis_client
        self._settings = settings
        self._local: TTLCache[str, int] = TTLCache(
            maxsize=COOLDOWN_LOCAL_CACHE_MAX_SIZE,
            ttl=COOLDOWN_LOCAL_CACHE_MAX_TTL_SEC,
        )

    @property
    def _debug_read_only(self) -> bool:
        return self._settings is not None and self._settings.debug_read_only

    # ------------------------------------------------------------------
    # Single-key API
    # ------------------------------------------------------------------
    async def is_cooling_down(
        self, eqp_id: str, category: str, metric: str
    ) -> bool:
        key = self._make_key(eqp_id, category, metric)
        # Debug mode: local-only, never touch Redis. The point of debug
        # mode is a self-contained single-run view; reading prod Redis
        # cooldowns would leak cross-run state into the debug session.
        if self._debug_read_only:
            return key in self._local
        try:
            return await self._redis.client.exists(key) > 0
        except _REDIS_UNAVAILABLE as e:
            logger.warning(
                "cooldown_check_redis_unavailable_use_local",
                key=key,
                error=str(e),
            )
            return key in self._local

    async def set_cooldown(
        self,
        eqp_id: str,
        category: str,
        metric: str,
        cooldown_minutes: int,
    ) -> None:
        key = self._make_key(eqp_id, category, metric)
        ttl_sec = cooldown_minutes * 60
        # Write local first so a Redis failure cannot skip it.
        self._local[key] = 1
        if self._debug_read_only:
            logger.warning(
                "debug_would_set_cooldown",
                key=key,
                ttl_sec=ttl_sec,
                reason="debug_read_only=true — Redis SETEX suppressed",
            )
            return
        try:
            await self._redis.client.setex(key, ttl_sec, "1")
        except _REDIS_UNAVAILABLE as e:
            logger.warning(
                "cooldown_set_redis_unavailable_local_only",
                key=key,
                error=str(e),
            )

    async def clear_cooldown(
        self, eqp_id: str, category: str, metric: str
    ) -> None:
        key = self._make_key(eqp_id, category, metric)
        self._local.pop(key, None)
        if self._debug_read_only:
            return  # local cleared, Redis untouched
        try:
            await self._redis.client.delete(key)
        except _REDIS_UNAVAILABLE:
            pass  # local is already cleared

    # ------------------------------------------------------------------
    # Batch API (single round-trip for up to N keys)
    # ------------------------------------------------------------------
    async def is_cooling_down_batch(
        self, checks: list[tuple[str, str, str]]
    ) -> dict[tuple[str, str, str], bool]:
        if self._debug_read_only:
            return {c: (self._make_key(*c) in self._local) for c in checks}
        try:
            async with self._redis.client.pipeline(transaction=False) as pipe:
                for eqp_id, cat, met in checks:
                    pipe.exists(self._make_key(eqp_id, cat, met))
                results = await pipe.execute()
            return {c: bool(r) for c, r in zip(checks, results, strict=True)}
        except _REDIS_UNAVAILABLE as e:
            logger.warning(
                "cooldown_batch_redis_unavailable_use_local",
                count=len(checks),
                error=str(e),
            )
            return {c: (self._make_key(*c) in self._local) for c in checks}

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _make_key(self, eqp_id: str, category: str, metric: str) -> str:
        return f"{self._redis.key_prefix}:cooldown:{eqp_id}:{category}:{metric}"
