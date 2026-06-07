"""Build repositories from a connected ``InfraContext``.

Also ensures schema invariants that must hold before the service accepts
traffic: it creates an EMPTY ``RESOURCE_MONITOR_PROFILE`` collection if it
does not yet exist and the unique index on the profile scope triple. Profile
documents are inserted manually (JSON) — startup does NOT seed a default
profile. In ``debug_read_only`` mode this schema-init is skipped entirely
(use ``scripts/create-profile-collection.ps1`` to create the collection).
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
            "debug_read_only_skip_schema_init",
            collection=COLL_PROFILE,
            reason="debug_read_only=true — must not mutate prod schema "
            "(use scripts/create-profile-collection.ps1 to create it manually)",
        )
    else:
        # Ensure the collection exists EMPTY. createIndex below also creates it
        # implicitly, but we create it explicitly so an empty collection is
        # guaranteed even before any document is inserted, and the action is
        # logged. Profiles are inserted manually (JSON) — startup no longer
        # seeds a default profile.
        existing = await db.list_collection_names()
        if COLL_PROFILE not in existing:
            await db.create_collection(COLL_PROFILE)
            logger.info("profile_collection_created", collection=COLL_PROFILE)
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
