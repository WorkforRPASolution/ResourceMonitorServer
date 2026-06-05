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
    ProfileNotFoundError,
    ProfileVersionConflictError,
    Scope,
    fold_profiles,
    lint_effective,
    utcnow,
    validate_effective,
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


def _specificity_rank(scope: Scope) -> int:
    """0=global, 1=process, 2=process+model, 3=full — for base→specific fold."""
    if scope.process == "*":
        return 0
    if scope.eqp_model == "*":
        return 1
    if scope.eqp_id == "*":
        return 2
    return 3


class ProfileRepository:
    def __init__(self, collection: AsyncIOMotorCollection) -> None:
        self._collection = collection
        # Bounded LRU + TTL cache of the *effective* (folded) profile, keyed by
        # the (process, model, eqpId) bucket — prevents unbounded growth under
        # high eqpId cardinality.
        self._resolve_cache: TTLCache[str, MonitorProfile] = TTLCache(
            maxsize=PROFILE_CACHE_MAX_SIZE, ttl=PROFILE_CACHE_TTL_SEC
        )

    # ------------------------------------------------------------------
    # Create / write
    # ------------------------------------------------------------------
    async def create(self, profile: MonitorProfile) -> str:
        try:
            result = await self._collection.insert_one(profile.to_mongo())
        except DuplicateKeyError as e:
            raise ProfileAlreadyExistsError(profile.scope) from e
        except _MONGO_UNAVAILABLE_EXC as e:
            raise MongoUnavailableError(f"create: {e}") from e
        self._resolve_cache.clear()
        return str(result.inserted_id)

    async def upsert(self, profile: MonitorProfile) -> None:
        """Unconditional replace-or-insert by exact scope (used by the seeder,
        which is the system of record — no optimistic lock). Clears the cache."""
        try:
            await self._collection.replace_one(
                self._scope_exact_filter(profile.scope), profile.to_mongo(), upsert=True
            )
        except _MONGO_UNAVAILABLE_EXC as e:
            raise MongoUnavailableError(f"upsert: {e}") from e
        self._resolve_cache.clear()

    async def replace_with_version(
        self, profile: MonitorProfile, expected_version: int
    ) -> int:
        """Optimistic-locked whole-overlay replace for operator edits.

        Bumps ``governance.version`` to ``expected_version + 1`` and returns it.
        Raises ``ProfileVersionConflictError`` on a stale version (409) or
        ``ProfileNotFoundError`` if the scope has no document (404).
        """
        new_version = expected_version + 1
        doc = profile.to_mongo()
        doc["governance"]["version"] = new_version
        # Refresh change-tracking metadata so governance reflects this edit
        # (and so the seeder can tell an operator-edited profile from the
        # code-owned default — see seed.seed_default_profile).
        doc["governance"]["updated_at"] = utcnow().isoformat()
        doc["governance"]["updated_by"] = "api"
        try:
            result = await self._collection.update_one(
                {
                    **self._scope_exact_filter(profile.scope),
                    "governance.version": expected_version,
                },
                {"$set": doc},
            )
        except _MONGO_UNAVAILABLE_EXC as e:
            raise MongoUnavailableError(f"replace_with_version: {e}") from e
        if result.matched_count == 0:
            await self._raise_write_conflict(profile.scope, expected_version)
        self._resolve_cache.clear()
        return new_version

    async def delete_by_scope(
        self, scope: Scope, expected_version: int | None = None
    ) -> None:
        """Delete a scope's overlay document. With ``expected_version`` the
        delete is optimistic-locked (409 on stale, 404 if absent)."""
        filter_ = self._scope_exact_filter(scope)
        if expected_version is not None:
            filter_["governance.version"] = expected_version
        try:
            result = await self._collection.delete_one(filter_)
        except _MONGO_UNAVAILABLE_EXC as e:
            raise MongoUnavailableError(f"delete_by_scope: {e}") from e
        if result.deleted_count == 0:
            await self._raise_write_conflict(scope, expected_version)
        self._resolve_cache.clear()

    async def _raise_write_conflict(
        self, scope: Scope, expected_version: int | None
    ) -> None:
        """Disambiguate a no-op write: missing document → NotFound, present but
        version-mismatched → VersionConflict."""
        existing = await self.find_by_scope(scope)
        if existing is None:
            raise ProfileNotFoundError(scope)
        raise ProfileVersionConflictError(scope, expected_version or 0)

    # ------------------------------------------------------------------
    # Lookup
    # ------------------------------------------------------------------
    async def find_by_scope(self, scope: Scope) -> MonitorProfile | None:
        """Load the single overlay document for an *exact* scope triple."""
        try:
            doc = await self._collection.find_one(self._scope_exact_filter(scope))
        except _MONGO_UNAVAILABLE_EXC as e:
            raise MongoUnavailableError(f"find_by_scope: {e}") from e
        return MonitorProfile.from_mongo(doc) if doc is not None else None

    async def collect_scope_docs(
        self, process: str, eqp_model: str, eqp_id: str
    ) -> list[MonitorProfile]:
        """Fetch every scope document covering (process, model, eqpId) in ONE
        ``$or`` query, ordered base→specific (avoids N+1)."""
        or_clauses = [
            {"scope.process": p, "scope.eqpModel": m, "scope.eqpId": e}
            for (p, m, e) in self._cascade_triples(process, eqp_model, eqp_id)
        ]
        try:
            cursor = self._collection.find({"$or": or_clauses})
            docs = await cursor.to_list(None)
        except _MONGO_UNAVAILABLE_EXC as e:
            raise MongoUnavailableError(f"collect_scope_docs: {e}") from e
        profiles = [MonitorProfile.from_mongo(d) for d in docs]
        profiles.sort(key=lambda p: _specificity_rank(p.scope))
        return profiles

    async def resolve_profile(
        self, process: str, eqp_model: str, eqp_id: str
    ) -> MonitorProfile | None:
        """Return the cascade-folded **effective** profile for an equipment.

        Collects all matching scope documents, folds them base→specific (narrower
        wins whole-object; ``enabled`` AND-folded), validates reference integrity
        on the result (logged, not raised — analysis must survive a bad overlay),
        and caches the effective profile under the bucket key. Returns ``None``
        when no scope document matches at all.
        """
        cache_key = f"{process}:{eqp_model}:{eqp_id}"
        cached = self._resolve_cache.get(cache_key)
        if cached is not None:
            return cached

        docs = await self.collect_scope_docs(process, eqp_model, eqp_id)
        if not docs:
            return None
        effective = fold_profiles(
            docs, Scope(process=process, eqp_model=eqp_model, eqp_id=eqp_id)
        )
        errors = validate_effective(effective)
        if errors:
            logger.warning(
                "effective_profile_invalid",
                process=process,
                eqp_model=eqp_model,
                eqp_id=eqp_id,
                errors=errors,
            )
        warnings = lint_effective(effective)
        if warnings:
            logger.warning(
                "effective_profile_lint",
                process=process,
                eqp_model=eqp_model,
                eqp_id=eqp_id,
                warnings=warnings,
            )
        self._resolve_cache[cache_key] = effective
        return effective

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _scope_exact_filter(scope: Scope) -> dict[str, Any]:
        """Exact-triple identity filter (matches wildcards literally). Used for
        single-document identity — find/replace/delete of one scope's overlay."""
        return {
            "scope.process": scope.process,
            "scope.eqpModel": scope.eqp_model,
            "scope.eqpId": scope.eqp_id,
        }

    @staticmethod
    def _cascade_triples(
        process: str, eqp_model: str, eqp_id: str
    ) -> list[tuple[str, str, str]]:
        """The exact ancestor scope triples to fold, broadest→narrowest, deduped."""
        triples: list[tuple[str, str, str]] = [("*", "*", "*")]
        if process != "*":
            triples.append((process, "*", "*"))
            if eqp_model != "*":
                triples.append((process, eqp_model, "*"))
                if eqp_id != "*":
                    triples.append((process, eqp_model, eqp_id))
        deduped: list[tuple[str, str, str]] = []
        for t in triples:
            if t not in deduped:
                deduped.append(t)
        return deduped


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
