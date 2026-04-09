"""Redis 5.0.6 async client wrapper.

Redis 5.0.6 constraints this wrapper enforces:
- RESP3 is not supported → ``protocol=2`` is pinned
- ACLs do not exist (5.0.6 predates them) → only simple ``requirepass`` AUTH
- Commands we use: ``SETEX``, ``EXISTS``, ``DEL``, ``SCAN``, pipelines. Anything
  newer (``GETEX``, ``GETDEL``, ``COPY``, ``BITCOUNT BYTE|BIT``) is forbidden.
"""
from __future__ import annotations

import asyncio

import structlog
from redis.asyncio import Redis

from src.config.settings import AppSettings

logger = structlog.get_logger(__name__)


class RedisClient:
    def __init__(self, settings: AppSettings) -> None:
        self._settings = settings
        self._client: Redis | None = None

    @property
    def client(self) -> Redis:
        if self._client is None:
            raise RuntimeError("RedisClient.connect() must be called first")
        return self._client

    @property
    def key_prefix(self) -> str:
        return self._settings.redis_key_prefix

    async def connect(self) -> None:
        """Single-attempt connect. Raises on ping failure.

        Note: ``self._client`` is only set AFTER the ping succeeds, so a
        failed connect leaves the wrapper in a clean uninitialized state
        that ``connect_with_retry`` can safely call again.
        """
        password = self._settings.redis_password.get_secret_value() or None
        client = Redis.from_url(
            self._settings.redis_url,
            password=password,
            decode_responses=True,
            protocol=2,  # Redis 5.0.6 predates RESP3
        )
        await client.ping()
        self._client = client
        logger.info("redis_connected")

    async def connect_with_retry(
        self, max_attempts: int = 3, backoff: float = 1.0
    ) -> None:
        """Try to connect up to ``max_attempts`` times with linear backoff.

        v6 P0-2: brings Redis startup in line with Mongo's
        ``connect_with_retry`` pattern. Default 3 attempts with linear
        backoff (sleeps 1s then 2s between attempts) — total ~3s of sleeps
        plus 3 ping latencies, well under the 45s ZK startup budget.

        On failure: log a ``redis_connect_retry`` warning per attempt and
        re-raise the last exception so ``init_infra``'s ``close_partial``
        rollback runs.
        """
        last_err: Exception | None = None
        for attempt in range(1, max_attempts + 1):
            try:
                await self.connect()
                return
            except Exception as e:
                last_err = e
                logger.warning(
                    "redis_connect_retry",
                    attempt=attempt,
                    max_attempts=max_attempts,
                    error=str(e),
                )
                if attempt < max_attempts:
                    await asyncio.sleep(backoff * attempt)
        assert last_err is not None
        raise last_err

    async def ping(self) -> bool:
        if self._client is None:
            return False
        try:
            return bool(await self._client.ping())
        except Exception as e:
            logger.warning("redis_ping_failed", error=str(e))
            return False

    async def close(self) -> None:
        if self._client is not None:
            try:
                await self._client.close()
            except Exception as e:
                logger.warning("redis_close_failed", error=str(e))
            finally:
                self._client = None
