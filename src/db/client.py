"""MongoDB async client wrapper built on motor.

v4 gotchas:
- `AsyncIOMotorClient.close()` is synchronous. Awaiting it raises `TypeError`.
- Connection failures at startup should not bring the whole service down —
  `connect_with_retry()` waits with linear backoff before giving up, letting
  Kubernetes' init ordering sort itself out.
"""
from __future__ import annotations

import asyncio

import structlog
from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase

from src.config.settings import AppSettings

logger = structlog.get_logger(__name__)


class MongoClient:
    """Owns a motor client and exposes the configured database."""

    def __init__(self, settings: AppSettings) -> None:
        self._settings = settings
        self._client: AsyncIOMotorClient | None = None
        self._db: AsyncIOMotorDatabase | None = None

    @property
    def db(self) -> AsyncIOMotorDatabase:
        if self._db is None:
            raise RuntimeError("MongoClient.connect_with_retry() must be called first")
        return self._db

    async def connect_with_retry(
        self, max_attempts: int = 5, backoff: float = 2.0
    ) -> None:
        """Try to connect up to ``max_attempts`` times with linear backoff.

        On each failure: log a warning and sleep ``backoff * attempt`` seconds.
        The last exception is re-raised if all attempts fail.
        """
        last_err: Exception | None = None
        for attempt in range(1, max_attempts + 1):
            try:
                client = AsyncIOMotorClient(
                    self._settings.mongo_uri.get_secret_value(),
                    serverSelectionTimeoutMS=5000,
                )
                # `ping` forces motor to actually contact the server.
                await client.admin.command("ping")
                self._client = client
                self._db = client[self._settings.mongo_db]
                logger.info("mongo_connected", database=self._settings.mongo_db)
                return
            except Exception as e:
                last_err = e
                logger.warning(
                    "mongo_connect_retry",
                    attempt=attempt,
                    max_attempts=max_attempts,
                    error=str(e),
                )
                if attempt < max_attempts:
                    await asyncio.sleep(backoff * attempt)
        assert last_err is not None
        raise last_err

    async def ping(self) -> bool:
        """True if the admin db responds to ``ping``. Never raises."""
        if self._client is None:
            return False
        try:
            await self._client.admin.command("ping")
            return True
        except Exception as e:
            logger.warning("mongo_ping_failed", error=str(e))
            return False

    async def close(self) -> None:
        """motor's ``close()`` is synchronous — do not await it."""
        if self._client is not None:
            try:
                self._client.close()  # NB: synchronous
            except Exception as e:
                logger.warning("mongo_close_failed", error=str(e))
            finally:
                self._client = None
                self._db = None
