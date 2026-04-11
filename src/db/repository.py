"""MongoDB repositories.

`ProfileRepository` — CRUD + cached `resolve_profile` (TTL-bounded).
`EqpInfoRepository` — read-only view of `EQP_INFO` (managed by the Akka server).

v4 design notes:
- The resolve cache is a `cachetools.TTLCache(maxsize=10000, ttl=300)`. With
  ~20K distinct eqpIds the cache can grow unbounded in the worst case, so we
  trade some hit rate for a hard memory ceiling.
- `EqpInfoRepository` filters on `onoff=1, webmanagerUse=1` so we never
  schedule analysis for decommissioned or manually-excluded equipment.
- `DuplicateKeyError` is translated to `ProfileAlreadyExistsError` so the API
  layer can return a clean 409 without leaking driver exceptions.

v6 P1-1 design notes:
- Every public async method translates connection-level driver exceptions
  (`ServerSelectionTimeoutError`, `NetworkTimeout`, `ConnectionFailure`)
  to `MongoUnavailableError`. This lets callers like the leader's
  `_do_redistribute` distinguish "infra blip, retry me" from "schema or
  permission error, fail the job".
- Other exceptions (e.g. `OperationFailure` for schema problems) propagate
  as-is.
"""
from __future__ import annotations

from typing import Any

import structlog
from cachetools import TTLCache
from motor.motor_asyncio import AsyncIOMotorCollection
from pymongo.errors import (
    ConnectionFailure,
    DuplicateKeyError,
    NetworkTimeout,
    ServerSelectionTimeoutError,
)

from src.config.constants import (
    PROFILE_CACHE_MAX_SIZE,
    PROFILE_CACHE_TTL_SEC,
)
from src.db.models import (
    MongoUnavailableError,
    MonitorProfile,
    ProfileAlreadyExistsError,
    Scope,
)

logger = structlog.get_logger(__name__)

# Tuple of pymongo exception classes that mean "the cluster is unreachable"
# rather than "your query was wrong". Translated to MongoUnavailableError
# at the repository boundary.
_MONGO_UNAVAILABLE_EXC = (
    ServerSelectionTimeoutError,
    NetworkTimeout,
    ConnectionFailure,
)


class ProfileRepository:
    def __init__(self, collection: AsyncIOMotorCollection) -> None:
        self._collection = collection
        # Bounded LRU + TTL cache prevents the unbounded growth we'd see with
        # a plain dict under high eqpId cardinality.
        self._resolve_cache: TTLCache[str, MonitorProfile] = TTLCache(
            maxsize=PROFILE_CACHE_MAX_SIZE, ttl=PROFILE_CACHE_TTL_SEC
        )

    # ------------------------------------------------------------------
    # Create / upsert
    # ------------------------------------------------------------------
    async def create(self, profile: MonitorProfile) -> str:
        try:
            result = await self._collection.insert_one(profile.to_mongo())
        except DuplicateKeyError as e:
            raise ProfileAlreadyExistsError(profile.scope) from e
        except _MONGO_UNAVAILABLE_EXC as e:
            raise MongoUnavailableError(f"create: {e}") from e
        return str(result.inserted_id)

    async def upsert(self, profile: MonitorProfile) -> None:
        """Replace-or-insert by scope. Clears the resolve cache on success."""
        filter_ = self._scope_nested_filter(profile.scope)
        try:
            await self._collection.replace_one(
                filter_, profile.to_mongo(), upsert=True
            )
        except _MONGO_UNAVAILABLE_EXC as e:
            raise MongoUnavailableError(f"upsert: {e}") from e
        self._resolve_cache.clear()

    # ------------------------------------------------------------------
    # Lookup
    # ------------------------------------------------------------------
    async def find_by_scope(self, scope: Scope) -> MonitorProfile | None:
        filter_ = self._scope_nested_filter(scope)
        try:
            doc = await self._collection.find_one(filter_)
        except _MONGO_UNAVAILABLE_EXC as e:
            raise MongoUnavailableError(f"find_by_scope: {e}") from e
        if doc is None:
            return None
        return MonitorProfile.from_mongo(doc)

    async def resolve_profile(
        self, process: str, eqp_model: str, eqp_id: str
    ) -> MonitorProfile | None:
        """Find the most specific profile covering (process, eqpModel, eqpId).

        Order of specificity (first hit wins):
            1. exact eqpId match
            2. exact (process, eqpModel)
            3. process only
            4. global wildcard (process="*")

        ``find_by_scope`` already translates Mongo-unavailable errors, so
        any raise here originates from a single ``find_by_scope`` call and
        propagates as ``MongoUnavailableError``.
        """
        cache_key = f"{process}:{eqp_model}:{eqp_id}"
        cached = self._resolve_cache.get(cache_key)
        if cached is not None:
            return cached

        # Try each specificity level. This is N+1-safe because we cache.
        candidates = [
            Scope(process=process, eqp_model=eqp_model, eqp_id=eqp_id),
            Scope(process=process, eqp_model=eqp_model),
            Scope(process=process),
            Scope(process="*"),
        ]
        for scope in candidates:
            profile = await self.find_by_scope(scope)
            if profile is not None:
                self._resolve_cache[cache_key] = profile
                return profile
        return None

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _scope_nested_filter(scope: Scope) -> dict[str, Any]:
        """Translate a `Scope` into a nested-field filter (`scope.*`).

        Uses EQP_INFO field names (`eqpModel`, `eqpId`). Wildcard fields are
        omitted rather than translated to regex.
        """
        filter_: dict[str, Any] = {"scope.process": scope.process}
        if scope.eqp_model != "*":
            filter_["scope.eqpModel"] = scope.eqp_model
        if scope.eqp_id != "*":
            filter_["scope.eqpId"] = scope.eqp_id
        return filter_


class EqpInfoRepository:
    """Read-only view of the `EQP_INFO` collection.

    We never write to this collection — it is owned by the Akka server. The
    `onoff` / `webmanagerUse` filter is applied on every read so analysis is
    only scheduled for active, managed equipment.
    """

    _ACTIVE_FILTER = {"onoff": 1, "webmanagerUse": 1}

    def __init__(self, collection: AsyncIOMotorCollection) -> None:
        self._collection = collection

    async def get_distinct_processes(self) -> list[str]:
        try:
            return await self._collection.distinct(
                "process", filter=self._ACTIVE_FILTER
            )
        except _MONGO_UNAVAILABLE_EXC as e:
            raise MongoUnavailableError(f"get_distinct_processes: {e}") from e

    async def count_active_by_process(self, process: str) -> int:
        try:
            return await self._collection.count_documents(
                {"process": process, **self._ACTIVE_FILTER}
            )
        except _MONGO_UNAVAILABLE_EXC as e:
            raise MongoUnavailableError(f"count_active_by_process: {e}") from e

    # Phase 1: bulk fetch for analysis engine (avoids N+1 per-eqpId queries)
    _EQUIPMENT_PROJECTION = {
        "_id": 0,
        "eqpId": 1,
        "eqpModel": 1,
        "process": 1,
        "localpc": 1,
        "ipAddr": 1,
        "line": 1,
        "category": 1,
    }

    async def get_active_equipment_by_process(
        self, process: str
    ) -> list[dict[str, Any]]:
        """Return all active equipment docs for a process.

        Used by the analysis engine to build an in-memory lookup dict
        ({eqpId: doc}) at the start of each analysis run, avoiding
        per-breach MongoDB queries.
        """
        try:
            cursor = self._collection.find(
                {"process": process, **self._ACTIVE_FILTER},
                self._EQUIPMENT_PROJECTION,
            )
            return await cursor.to_list(None)
        except _MONGO_UNAVAILABLE_EXC as e:
            raise MongoUnavailableError(
                f"get_active_equipment_by_process: {e}"
            ) from e
