"""Build repositories from a connected ``InfraContext``.

Also ensures schema invariants that must hold before the service accepts
traffic — notably the unique index on the profile scope triple. MongoDB
creates the collection implicitly on the first ``createIndex`` call, so
this is safe on a fresh EARS DB where ``RESOURCE_MONITOR_PROFILE`` does
not yet exist.
"""
from __future__ import annotations

from dataclasses import dataclass

import structlog
from pymongo import ASCENDING

from src.config.constants import COLL_EQP_INFO, COLL_PROFILE
from src.config.settings import AppSettings
from src.db.repository import EqpInfoRepository, ProfileRepository
from src.startup.infra import InfraContext

logger = structlog.get_logger(__name__)


@dataclass
class RepositoryContext:
    profile_repo: ProfileRepository
    eqp_info_repo: EqpInfoRepository


async def init_repos(
    infra: InfraContext, settings: AppSettings
) -> RepositoryContext:
    if infra.mongo is None:
        raise RuntimeError("init_repos requires a connected MongoClient")
    db = infra.mongo.db

    # Schema invariant: exactly one profile per (process, eqpModel, eqpId).
    # Without this index ProfileRepository.create()'s DuplicateKeyError path
    # cannot fire, and two concurrent create() calls could silently produce
    # duplicate documents. createIndex is idempotent — re-running is a no-op
    # ("all indexes already exist") — and implicitly creates the collection
    # on first deploy.
    #
    # Debug Read-Only mode: a debugging instance connects to production Mongo
    # assuming the index already exists (created by the real prod pods). It
    # MUST NOT mutate the production schema, so we skip the create_index call
    # entirely. The guard is intentionally explicit rather than "idempotent so
    # who cares" — writes into prod are never "who cares".
    if settings.debug_read_only:
        logger.warning(
            "debug_read_only_skip_create_index",
            collection=COLL_PROFILE,
            reason="debug_read_only=true — must not mutate prod schema",
        )
    else:
        await db[COLL_PROFILE].create_index(
            [
                ("scope.process", ASCENDING),
                ("scope.eqpModel", ASCENDING),
                ("scope.eqpId", ASCENDING),
            ],
            unique=True,
            name="uniq_scope",
        )
        logger.info("profile_unique_index_ensured", name="uniq_scope")

    return RepositoryContext(
        profile_repo=ProfileRepository(db[COLL_PROFILE]),
        eqp_info_repo=EqpInfoRepository(db[COLL_EQP_INFO]),
    )
