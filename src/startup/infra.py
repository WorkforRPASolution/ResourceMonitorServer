"""Infrastructure connection orchestration.

The lifespan in main.py is intentionally a thin wrapper that delegates to
``init_infra`` (and friends in this package). This makes the connection
sequence testable in isolation, lets us reuse the helpers in integration
tests, and keeps the lifespan readable.

Failure model:
- Connections happen sequentially. If any one fails, all previously-connected
  clients are closed before the exception propagates.
- Each ``close()`` is wrapped in try/except so that one failure does not
  prevent the others from running.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass

import structlog

from src.alert.email_client import EmailAlertClient
from src.cache.redis_client import RedisClient
from src.config.settings import AppSettings
from src.db.client import MongoClient
from src.distributed.zk_client import ZKClient
from src.es.client import ESClient

logger = structlog.get_logger(__name__)


@asynccontextmanager
async def startup_phase(name: str):
    """Log a phase boundary so startup is grep-able.

    Used by main.py to bracket each init step. The exception path re-raises
    after logging so the lifespan can run its rollback.
    """
    logger.info("startup_phase_begin", phase=name)
    try:
        yield
        logger.info("startup_phase_done", phase=name)
    except Exception as e:
        logger.error(
            "startup_phase_failed", phase=name, error=str(e), exc_info=True
        )
        raise


@dataclass
class InfraContext:
    """Bag of connected infrastructure clients.

    Set by ``init_infra``. The ``close_partial()`` method is the rollback
    used both on init failure and on lifespan shutdown.
    """

    es: ESClient | None = None
    mongo: MongoClient | None = None
    redis: RedisClient | None = None
    email: EmailAlertClient | None = None
    zk: ZKClient | None = None

    async def close_partial(self) -> None:
        """Close every populated client. Errors are logged and swallowed.

        Order is the reverse of init: ZK first (so leader/lock cleanup
        happens before MongoDB writes can race), then app HTTP clients,
        then storage.
        """
        for name, client in [
            ("zk", self.zk),
            ("email", self.email),
            ("redis", self.redis),
            ("mongo", self.mongo),
            ("es", self.es),
        ]:
            if client is None:
                continue
            try:
                await client.close()
            except Exception as e:
                logger.warning(f"{name}_close_failed", error=str(e))


async def init_infra(settings: AppSettings) -> InfraContext:
    """Connect every infra client. Roll back on failure.

    Sequential connect order:
        1. Elasticsearch  (analyzer's primary read source)
        2. MongoDB        (profiles + EQP_INFO)
        3. Redis          (cooldown)
        4. Email API      (alert sink)
        5. Zookeeper      (coordination — last because it's the most fragile)

    If any connect raises, every previously-connected client is closed before
    the exception propagates.

    Debug Read-Only mode: Zookeeper is NOT connected. A debug instance that
    registers itself as a ZK member would pollute the production cluster's
    membership and cause leader election to redistribute work to this
    debugger, and when the debugger exits it would flap partitioning again.
    ``infra.zk`` stays ``None`` and the lifespan skips every phase that
    depends on it.
    """
    ctx = InfraContext()
    try:
        ctx.es = ESClient(settings)
        await ctx.es.connect()

        ctx.mongo = MongoClient(settings)
        await ctx.mongo.connect_with_retry()

        ctx.redis = RedisClient(settings)
        await ctx.redis.connect_with_retry()

        ctx.email = EmailAlertClient(settings)
        await ctx.email.connect()

        if settings.debug_read_only:
            logger.warning(
                "debug_read_only_skip_zk_connect",
                reason="debug instance must not join prod ZK cluster",
            )
        else:
            ctx.zk = ZKClient(settings)
            await ctx.zk.connect()

        return ctx
    except Exception:
        await ctx.close_partial()
        raise
