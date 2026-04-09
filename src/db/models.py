"""Pydantic v2 models for MongoDB persistence.

Critical field name mapping (v4):
    Scope.eqp_model  ←→  EQP_INFO.eqpModel  (JSON API still uses "model")
    Scope.eqp_id     ←→  EQP_INFO.eqpId     (JSON API uses "eqpId")

The `model` JSON field is aliased because Pydantic v2 reserves identifiers
starting with `model_` (e.g. `model_config`, `model_dump`). Internally we use
`eqp_model`; externally (both incoming JSON and Mongo documents) we use the
field names the rest of the EARS system already expects.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


# ----------------------------------------------------------------------
# Domain exceptions
# ----------------------------------------------------------------------
class ProfileAlreadyExistsError(Exception):
    def __init__(self, scope: "Scope") -> None:
        super().__init__(f"Profile already exists for scope: {scope!r}")
        self.scope = scope


class ProfileNotFoundError(Exception):
    def __init__(self, scope: "Scope") -> None:
        super().__init__(f"Profile not found for scope: {scope!r}")
        self.scope = scope


class MongoUnavailableError(Exception):
    """v6 P1-1: raised by repository methods when the underlying Mongo
    driver reports a connection-level failure (ServerSelectionTimeoutError,
    NetworkTimeout, ConnectionFailure).

    Distinct from generic ``Exception`` so callers (in particular the
    leader's ``_do_redistribute``) can react differently — retry with
    backoff for an unavailable infra, but propagate other errors (schema,
    permission, malformed data) as straight job failures.
    """

    def __init__(self, msg: str) -> None:
        super().__init__(msg)


# ----------------------------------------------------------------------
# Value objects
# ----------------------------------------------------------------------
class Scope(BaseModel):
    """Hierarchical scope: process → eqpModel → eqpId.

    Wildcards (`"*"`) narrow from broadest to most specific. A profile's
    lookup order is: exact eqpId → exact (process, eqpModel) → process only
    → global wildcard.
    """

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    process: str
    eqp_model: str = Field(default="*", alias="model")
    eqp_id: str = Field(default="*", alias="eqpId")

    def to_mongo_query(self) -> dict[str, Any]:
        """Build a MongoDB filter for EQP_INFO / profile lookup.

        Uses EQP_INFO's field names (`eqpModel`, `eqpId`), not Pydantic aliases.
        Wildcard (`"*"`) values are omitted (not translated to regex).
        """
        q: dict[str, Any] = {"process": self.process}
        if self.eqp_model != "*":
            q["eqpModel"] = self.eqp_model
        if self.eqp_id != "*":
            q["eqpId"] = self.eqp_id
        return q

    def to_mongo(self) -> dict[str, Any]:
        """Serialize the scope itself for embedding inside a profile document."""
        return {
            "process": self.process,
            "eqpModel": self.eqp_model,
            "eqpId": self.eqp_id,
        }

    @classmethod
    def from_mongo(cls, doc: dict[str, Any]) -> "Scope":
        return cls(
            process=doc["process"],
            eqp_model=doc.get("eqpModel", "*"),
            eqp_id=doc.get("eqpId", "*"),
        )

    def __hash__(self) -> int:  # allow use as dict key in tests/caches
        return hash((self.process, self.eqp_model, self.eqp_id))


class ThresholdConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    warning: float
    critical: float
    cooldown_minutes: int


class MetricSchedule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    interval_minutes: int
    window_minutes: int


class AnalysisConfig(BaseModel):
    """Analysis rule for one metric pattern (supports wildcards like `*_core_load`)."""

    model_config = ConfigDict(extra="forbid")

    metric_pattern: str
    threshold: ThresholdConfig
    schedule: MetricSchedule


# ----------------------------------------------------------------------
# Aggregate root
# ----------------------------------------------------------------------
class MonitorProfile(BaseModel):
    """A set of analysis rules scoped to a portion of the equipment hierarchy."""

    model_config = ConfigDict(extra="forbid")

    id: str | None = None  # stringified ObjectId, populated by from_mongo
    scope: Scope
    analysis_configs: list[AnalysisConfig] = Field(default_factory=list)

    def to_mongo(self) -> dict[str, Any]:
        """Serialize for `insert_one` / `replace_one` (no `_id` — Mongo picks one)."""
        return {
            "scope": self.scope.to_mongo(),
            "analysis_configs": [
                ac.model_dump(mode="json") for ac in self.analysis_configs
            ],
        }

    @classmethod
    def from_mongo(cls, doc: dict[str, Any]) -> "MonitorProfile":
        """Deserialize a Mongo document.

        `_id` is converted to a hex string and stored on `.id`. `created_at` /
        `updated_at` are currently only used at the repository layer; they do
        not live on the domain model.
        """
        raw_id = doc.get("_id")
        id_str = str(raw_id) if raw_id is not None else None
        scope = Scope.from_mongo(doc["scope"])
        configs = [AnalysisConfig.model_validate(ac) for ac in doc.get("analysis_configs", [])]
        return cls(id=id_str, scope=scope, analysis_configs=configs)


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------
def utcnow() -> datetime:
    """Timezone-aware UTC now (testable alternative to `datetime.utcnow()`)."""
    return datetime.now(timezone.utc)
