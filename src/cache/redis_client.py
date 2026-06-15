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
from redis.asyncio.sentinel import Sentinel

from src.config.settings import AppSettings

logger = structlog.get_logger(__name__)


class RedisClient:
    def __init__(self, settings: AppSettings) -> None:
        self._settings = settings
        self._client: Redis | None = None
        # In sentinel mode this holds the Sentinel manager so close() can also
        # release its per-sentinel monitoring connections (the master client
        # only owns the master pool). None in url mode.
        self._sentinel: Sentinel | None = None

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

        Two modes (decided by ``redis_sentinels``):
        - **url** (default): single endpoint via ``Redis.from_url(redis_url)``.
        - **sentinel**: HA via ``Sentinel(...).master_for(master)`` — always
          resolves the *current* master so a failover is followed automatically.

        Note: ``self._client`` is only set AFTER the ping succeeds, so a
        failed connect leaves the wrapper in a clean uninitialized state
        that ``connect_with_retry`` can safely call again.
        """
        password = self._settings.redis_password.get_secret_value() or None
        sentinel: Sentinel | None = None
        if self._settings.redis_sentinels:
            client, sentinel = self._sentinel_master(password)
            mode = "sentinel"
        else:
            client = Redis.from_url(
                self._settings.redis_url,
                password=password,
                decode_responses=True,
                protocol=2,  # Redis 5.0.6 predates RESP3
            )
            mode = "url"
        await client.ping()
        # only set state AFTER a successful ping (clean rollback for retry)
        self._client = client
        self._sentinel = sentinel
        logger.info("redis_connected", mode=mode)

    def _sentinel_master(self, password: str | None) -> tuple[Redis, Sentinel]:
        """Build a Redis client bound to the Sentinel master pool.

        ``redis_sentinels`` items are ``host[:port]`` (port defaults to 26379,
        the Sentinel port). Data-node connections always use ``redis_password``.
        Sentinel auth uses ``redis_sentinel_password`` ONLY when explicitly set —
        it does NOT fall back to ``redis_password``. This is deliberate: a
        Sentinel often has no ``requirepass`` even when the data nodes do
        (e.g. redis-ha with ``auth=true`` but ``sentinel.auth=false``); sending
        the data password to such a Sentinel is rejected with
        "Client sent AUTH, but no password is set". If your Sentinels DO require
        auth, set ``redis_sentinel_password`` explicitly. DB comes from
        ``redis_db`` (URL ``/N`` is not used in this mode)."""
        sentinels: list[tuple[str, int]] = []
        for entry in self._settings.redis_sentinels:
            host, sep, port = entry.strip().rpartition(":")
            if not sep:  # no ":" → only a host was given
                host, port = port, "26379"
            sentinels.append((host, int(port)))
        # No fallback to the data password — empty means "send no AUTH to sentinels".
        sentinel_pw = self._settings.redis_sentinel_password.get_secret_value() or None
        sentinel = Sentinel(
            sentinels,
            sentinel_kwargs={"password": sentinel_pw} if sentinel_pw else None,
            password=password,  # data-node (master/replica) AUTH
            decode_responses=True,
            protocol=2,
        )
        master = sentinel.master_for(
            self._settings.redis_sentinel_master,
            db=self._settings.redis_db,
            password=password,
            decode_responses=True,
            protocol=2,
        )
        return master, sentinel

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
                await self._client.aclose()  # close() deprecated in redis-py 5.0.1+
            except Exception as e:
                logger.warning("redis_close_failed", error=str(e))
            finally:
                self._client = None
        # sentinel mode: also release the per-sentinel monitoring connections
        # (the master client above only owns the master pool).
        if self._sentinel is not None:
            try:
                await asyncio.gather(
                    *(s.aclose() for s in self._sentinel.sentinels),
                    return_exceptions=True,
                )
            except Exception as e:
                logger.warning("redis_sentinel_close_failed", error=str(e))
            finally:
                self._sentinel = None
