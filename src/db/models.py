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

from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.analyzer import fact_catalog as fc
from src.analyzer.fact_catalog import FactType


# ----------------------------------------------------------------------
# Domain exceptions
# ----------------------------------------------------------------------
class ProfileAlreadyExistsError(Exception):
    def __init__(self, scope: Scope) -> None:
        super().__init__(f"Profile already exists for scope: {scope!r}")
        self.scope = scope


class ProfileNotFoundError(Exception):
    def __init__(self, scope: Scope) -> None:
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
    def from_mongo(cls, doc: dict[str, Any]) -> Scope:
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
    def from_mongo(cls, doc: dict[str, Any]) -> MonitorProfile:
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
    return datetime.now(UTC)


# ======================================================================
# v2 schema (measures / rules / notify). Added alongside the v1 model
# during migration; the v2 MonitorProfile aggregate + cross-object
# validators land in the atomic switch (see plan). Field/structure specs:
# SCHEMA.md §1-§3, fact catalog: src/analyzer/fact_catalog.py.
# ======================================================================
Operator = Literal[">=", ">", "<=", "<", "==", "!=", "trend=="]
Severity = Literal["WARNING", "CRITICAL"]
Direction = Literal["above", "below", "high", "low"]
DeltaMode = Literal["last_minus_first", "max_minus_min"]
GrowthUnit = Literal["per_hour", "per_day"]
Combine = Literal["AND", "OR"]
GroupByKey = Literal["eqpId", "proc"]
ExpandMode = Literal["scalar", "instance"]
MetricKind = Literal["gauge", "counter", "cumulative"]
Quantifier = Literal["any", "all", "count"]

_WILDCARD_CHARS = ("*", "?", "[")


class Bucketing(BaseModel):
    """Sub-window time bucketing shared by date-histogram facts."""

    model_config = ConfigDict(extra="forbid")

    seconds: int
    points: int | None = None


class BaselineSpec(BaseModel):
    """Historical baseline params shared by baseline_dev (Phase 3)."""

    model_config = ConfigDict(extra="forbid")

    days: int = 7
    same_hour: bool = True
    min_points: int = 30
    deviation_floor: float = 1.0


class Fact(BaseModel):
    """One named output a measure produces. ``type`` is the fact name a rule
    references via ``measureId.type``."""

    model_config = ConfigDict(extra="forbid")

    type: FactType
    over: float | None = None              # spike_count / duration: event boundary
    direction: Direction | None = None     # spike/duration: above|below; zscore: high|low
    mode: DeltaMode | None = None          # delta
    unit: GrowthUnit | None = None         # growth_rate

    @model_validator(mode="after")
    def _per_type_params(self) -> Fact:
        t = self.type
        if t in fc.REQUIRES_OVER_DIRECTION:
            if self.over is None or self.direction is None:
                raise ValueError(f"{t.value}: 'over' and 'direction' are required")
            if self.direction not in ("above", "below"):
                raise ValueError(f"{t.value}: direction must be 'above' or 'below'")
        if t is FactType.DELTA and self.mode is None:
            self.mode = "last_minus_first"
        if t is FactType.GROWTH_RATE and self.unit is None:
            self.unit = "per_hour"
        if t is FactType.ZSCORE:
            if self.direction is None:
                self.direction = "high"
            elif self.direction not in ("high", "low"):
                raise ValueError("zscore: direction must be 'high' or 'low'")
        return self


class Measure(BaseModel):
    """What to measure and how — produces facts. Owns the aggregation window;
    the evaluation interval lives on rules."""

    model_config = ConfigDict(extra="forbid")

    id: str
    category: str
    metric: str
    proc: str = "@system"
    window_minutes: int
    group_by: list[GroupByKey] = Field(default_factory=lambda: ["eqpId"])
    expand: ExpandMode = "scalar"
    metric_kind: MetricKind | None = None
    bucketing: Bucketing | None = None
    baseline: BaselineSpec | None = None
    facts: list[Fact]

    @model_validator(mode="after")
    def _consistency(self) -> Measure:
        if not self.facts:
            raise ValueError(f"measure '{self.id}': at least one fact required")
        types = [f.type for f in self.facts]
        if len(types) != len(set(types)):
            raise ValueError(f"measure '{self.id}': duplicate fact type")
        tset = set(types)
        if tset & fc.NEEDS_BUCKETING and self.bucketing is None:
            raise ValueError(f"measure '{self.id}': time-bucketed fact requires 'bucketing'")
        if tset & fc.NEEDS_POINTS and (self.bucketing is None or self.bucketing.points is None):
            raise ValueError(f"measure '{self.id}': moving_avg/trend require bucketing.points")
        if tset & fc.NEEDS_BASELINE and self.baseline is None:
            raise ValueError(f"measure '{self.id}': baseline_dev requires 'baseline'")
        if (
            self.bucketing
            and self.bucketing.points is not None
            and self.bucketing.seconds * self.bucketing.points > self.window_minutes * 60
        ):
            raise ValueError(
                f"measure '{self.id}': bucketing.seconds*points exceeds window"
            )
        # auto-derive grouping/expansion
        if self.proc == "*" and "proc" not in self.group_by:
            self.group_by = [*self.group_by, "proc"]
        if any(c in self.metric for c in _WILDCARD_CHARS) and self.expand == "scalar":
            self.expand = "instance"
        return self


class Condition(BaseModel):
    """One ``when`` clause: compare a fact (``measureId.type``) against a value."""

    model_config = ConfigDict(extra="forbid")

    fact: str
    op: Operator
    value: float | str
    quantifier: Quantifier = "any"
    count_min: int | None = None

    @model_validator(mode="after")
    def _checks(self) -> Condition:
        if "." not in self.fact:
            raise ValueError(f"fact '{self.fact}' must be 'measureId.type'")
        if self.quantifier == "count" and self.count_min is None:
            raise ValueError("quantifier 'count' requires 'count_min'")
        if self.op == "trend==":
            if not isinstance(self.value, str):
                raise ValueError("op 'trend==' requires a string value")
        elif not isinstance(self.value, (int, float)):
            raise ValueError(f"op '{self.op}' requires a numeric value")
        return self


class Rule(BaseModel):
    """When to alert — references facts, owns the evaluation interval."""

    model_config = ConfigDict(extra="forbid")

    id: str
    interval_minutes: int
    severity: Severity
    combine: Combine = "AND"
    when: list[Condition]
    notify: str = "default"

    @model_validator(mode="after")
    def _nonempty(self) -> Rule:
        if not self.when:
            raise ValueError(f"rule '{self.id}': 'when' must be non-empty")
        return self


class NotifyChannel(BaseModel):
    """How to deliver — referenced by rules by name."""

    model_config = ConfigDict(extra="forbid")

    cooldown_minutes: int
    email_code: str = "RESOURCE_MONITOR"
    email_subcode: str | None = None


class Governance(BaseModel):
    """Change-tracking metadata (optimistic-lock token = version)."""

    model_config = ConfigDict(extra="forbid")

    version: int = 1
    updated_by: str = ""
    updated_at: datetime = Field(default_factory=utcnow)
    change_reason: str = ""
