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

import hashlib
import json
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


class ProfileVersionConflictError(Exception):
    """v2 optimistic-lock failure: the stored ``governance.version`` did not
    match the version the writer expected (a concurrent edit won the race).
    The API layer maps this to HTTP 409.
    """

    def __init__(self, scope: Scope, expected_version: int) -> None:
        super().__init__(
            f"version conflict for {scope!r}: expected version {expected_version}"
        )
        self.scope = scope
        self.expected_version = expected_version


class ProfileValidationError(Exception):
    """v2 effective-profile reference-integrity failure. Carries one message
    per broken reference (with a ``rules[i].when[j].fact`` field path) so the
    API can surface HTTP 422 with inline form errors (ADMIN-UI §6)."""

    def __init__(self, errors: list[str]) -> None:
        super().__init__("; ".join(errors))
        self.errors = errors


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


# ----------------------------------------------------------------------
# Aggregate root (v2)
# ----------------------------------------------------------------------
# The v2 ``MonitorProfile`` aggregate is defined at the END of this module,
# after its building blocks (Measure/Rule/NotifyChannel/Governance). The v1
# ThresholdConfig/MetricSchedule/AnalysisConfig types were removed in the v2
# schema switch (see SCHEMA.md §13); the class name MonitorProfile and the
# to_mongo()/from_mongo() contract are preserved so repository.py is unchanged
# at the import boundary.


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
        if self.quantifier == "count" and (self.count_min is None or self.count_min < 1):
            # count_min must be >= 1; count_min=0 would make the condition fire
            # unconditionally (n >= 0 is always true) → silent always-alert.
            raise ValueError("quantifier 'count' requires count_min >= 1")
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


# ======================================================================
# v2 aggregate root + cascade fold + effective validation
# ======================================================================
class MonitorProfile(BaseModel):
    """A monitoring profile for one scope (one Mongo document).

    Holds the three v2 layers — ``measures`` (잰다), ``rules`` (판단) and
    ``notify`` (알린다) — plus the ``enabled`` flag and ``governance`` token.
    A *stored* document is usually a sparse overlay; the *effective* profile an
    equipment actually runs is the cascade fold of every matching scope (see
    :func:`fold_profiles`). The class name and the to_mongo/from_mongo contract
    are kept stable from v1 so the repository boundary needs no edits.
    """

    model_config = ConfigDict(extra="forbid")

    id: str | None = None  # stringified ObjectId, populated by from_mongo
    scope: Scope
    enabled: bool = True
    governance: Governance = Field(default_factory=Governance)
    measures: list[Measure] = Field(default_factory=list)
    rules: list[Rule] = Field(default_factory=list)
    notify: dict[str, NotifyChannel] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _doc_consistency(self) -> MonitorProfile:
        mids = [m.id for m in self.measures]
        if len(mids) != len(set(mids)):
            raise ValueError("duplicate measure id in profile")
        rids = [r.id for r in self.rules]
        if len(rids) != len(set(rids)):
            raise ValueError("duplicate rule id in profile")
        return self

    # -- serialization -------------------------------------------------
    def to_mongo(self) -> dict[str, Any]:
        """Serialize for insert/replace (no ``_id`` — Mongo assigns one)."""
        return {
            "scope": self.scope.to_mongo(),
            "enabled": self.enabled,
            "governance": self.governance.model_dump(mode="json"),
            "measures": [m.model_dump(mode="json") for m in self.measures],
            "rules": [r.model_dump(mode="json") for r in self.rules],
            "notify": {k: v.model_dump(mode="json") for k, v in self.notify.items()},
        }

    @classmethod
    def from_mongo(cls, doc: dict[str, Any]) -> MonitorProfile:
        raw_id = doc.get("_id")
        id_str = str(raw_id) if raw_id is not None else None
        gov_doc = doc.get("governance")
        return cls(
            id=id_str,
            scope=Scope.from_mongo(doc["scope"]),
            enabled=doc.get("enabled", True),
            governance=Governance.model_validate(gov_doc) if gov_doc else Governance(),
            measures=[Measure.model_validate(m) for m in doc.get("measures", [])],
            rules=[Rule.model_validate(r) for r in doc.get("rules", [])],
            notify={
                k: NotifyChannel.model_validate(v)
                for k, v in doc.get("notify", {}).items()
            },
        )

    # -- hashing / identity -------------------------------------------
    def structural_mongo(self) -> dict[str, Any]:
        """``to_mongo()`` minus the volatile ``governance`` block — the basis
        for seed drift detection so a fresh ``updated_at`` never forces a
        spurious reseed that would stomp operator edits."""
        doc = self.to_mongo()
        doc.pop("governance", None)
        return doc

    def effective_signature(self) -> str:
        """Stable hash of the *behavioural* content (measures/rules/notify/
        enabled), independent of scope/governance. Equipment whose effective
        profiles share a signature are analysed with a single ES query (engine
        bucketing). Lists are sorted by id/name for order-stability."""
        payload = {
            "enabled": self.enabled,
            "measures": sorted(
                (m.model_dump(mode="json") for m in self.measures),
                key=lambda m: m["id"],
            ),
            "rules": sorted(
                (r.model_dump(mode="json") for r in self.rules),
                key=lambda r: r["id"],
            ),
            "notify": {
                k: self.notify[k].model_dump(mode="json") for k in sorted(self.notify)
            },
        }
        blob = json.dumps(payload, sort_keys=True, default=str)
        return hashlib.sha256(blob.encode()).hexdigest()


def fold_profiles(ordered: list[MonitorProfile], target_scope: Scope) -> MonitorProfile:
    """Fold scope documents broadest→most-specific into one effective profile
    (SCHEMA.md §6).

    ``ordered`` must be sorted base→specific, e.g. ``[(*,*,*), (p,*,*), (p,m,*),
    (p,m,e)]``. Merge is by key — ``measures`` by ``measure.id``, ``rules`` by
    ``rule.id``, ``notify`` by channel name — and the more-specific object
    replaces the whole entry (no field-level partial merge). ``enabled`` folds
    with AND: any disabled level disables the effective profile.
    """
    measures: dict[str, Measure] = {}
    rules: dict[str, Rule] = {}
    notify: dict[str, NotifyChannel] = {}
    enabled = True
    governance = Governance()
    for prof in ordered:
        enabled = enabled and prof.enabled
        for m in prof.measures:
            measures[m.id] = m
        for r in prof.rules:
            rules[r.id] = r
        for name, ch in prof.notify.items():
            notify[name] = ch
        governance = prof.governance
    return MonitorProfile(
        scope=target_scope,
        enabled=enabled,
        governance=governance,
        measures=list(measures.values()),
        rules=list(rules.values()),
        notify=notify,
    )


def validate_effective(profile: MonitorProfile) -> list[str]:
    """Reference-integrity check on a *folded* effective profile (SCHEMA.md §5
    items 5/7/8, §6.4). Returns field-path error messages (empty when valid).

    Run this on the effective profile, never on a sparse overlay — an overlay
    alone may legitimately reference a measure that lives in a parent scope.
    """
    errors: list[str] = []
    measures_by_id = {m.id: m for m in profile.measures}
    for ri, rule in enumerate(profile.rules):
        if rule.notify not in profile.notify:
            errors.append(f"rules[{ri}].notify: channel '{rule.notify}' is not defined")
        referenced: set[str] = set()
        for ci, cond in enumerate(rule.when):
            mid, _, ftype = cond.fact.partition(".")
            measure = measures_by_id.get(mid)
            if measure is None:
                errors.append(f"rules[{ri}].when[{ci}].fact: measure '{mid}' not found")
                continue
            referenced.add(mid)
            by_type = {f.type.value: f.type for f in measure.facts}
            if ftype not in by_type:
                errors.append(
                    f"rules[{ri}].when[{ci}].fact: measure '{mid}' declares "
                    f"no fact '{ftype}'"
                )
                continue
            if not fc.op_allowed(by_type[ftype], cond.op):
                errors.append(
                    f"rules[{ri}].when[{ci}].op: '{cond.op}' is not allowed for "
                    f"fact '{ftype}'"
                )
        for mid in referenced:
            measure = measures_by_id[mid]
            if rule.interval_minutes > measure.window_minutes:
                errors.append(
                    f"rules[{ri}].interval_minutes ({rule.interval_minutes}) exceeds "
                    f"measure '{mid}'.window_minutes ({measure.window_minutes})"
                )
        # All measures a rule references must live on ONE proc dimension. The
        # engine evaluates a rule per (eqp, proc) target; measures with different
        # proc produce disjoint proc keys, so an AND rule across them could never
        # fire (silent lost breach). Reject the misconfiguration at write time.
        procs = {measures_by_id[mid].proc for mid in referenced}
        if len(procs) > 1:
            errors.append(
                f"rules[{ri}]: conditions span measures with differing proc "
                f"{sorted(procs)}; a rule must reference one proc dimension"
            )
    return errors
