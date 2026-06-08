"""Seed the default monitoring profile on startup.

Hash-based change detection: if the stored profile's *structural* content
(measures/rules/notify/enabled — governance excluded) matches the built-in
default, we leave it alone so operators can hand-edit without the service
stomping their changes on restart. ``governance`` is excluded from the hash
because its ``updated_at`` changes on every build and would force an endless
reseed.
"""
from __future__ import annotations

import hashlib
import json

import structlog
from pymongo.errors import DuplicateKeyError

from src.db.models import (
    Condition,
    Fact,
    Governance,
    Measure,
    MonitorProfile,
    NotifyChannel,
    Rule,
    Scope,
)
from src.db.repository import ProfileRepository

logger = structlog.get_logger(__name__)


def build_default_profile() -> MonitorProfile:
    """The built-in global wildcard profile (Phase-1 fact types only).

    Covers the common resource classes ResourceAgent collects. Operators tune
    or override per-scope via the profile CRUD API; this is the safe baseline.
    """
    measures = [
        Measure(id="cpu", category="cpu", metric="total_used_pct", window_minutes=15,
                facts=[Fact(type="max"), Fact(type="p95"),
                       Fact(type="spike_count", over=90, direction="above")]),
        Measure(id="mem_used", category="memory", metric="total_used_pct",
                window_minutes=15, facts=[Fact(type="max")]),
        Measure(id="mem_free", category="memory", metric="total_free_pct",
                window_minutes=15, facts=[Fact(type="min")]),
        Measure(id="disk", category="disk", metric="*", window_minutes=30,
                facts=[Fact(type="max")]),
        Measure(id="temp", category="temperature", metric="*", window_minutes=15,
                facts=[Fact(type="max")]),
        Measure(id="fan", category="fan", metric="*", window_minutes=15,
                facts=[Fact(type="min")]),
        Measure(id="volt_vcore", category="voltage", metric="CPU_Vcore",
                window_minutes=15, facts=[Fact(type="min"), Fact(type="max")]),
        Measure(id="gpu_load", category="gpu", metric="*_core_load",
                window_minutes=15, facts=[Fact(type="p95")]),
        Measure(id="ssd_life", category="storage_smart", metric="*_remaining_life",
                window_minutes=60, facts=[Fact(type="min")]),
        Measure(id="disk_health", category="storage_health", metric="*_status",
                window_minutes=60, facts=[Fact(type="max")]),
        Measure(id="proc_required", category="process_watch", metric="required",
                proc="*", window_minutes=5, facts=[Fact(type="min")]),
        Measure(id="proc_forbidden", category="process_watch", metric="forbidden",
                proc="*", window_minutes=5, facts=[Fact(type="max")]),
    ]
    rules = [
        Rule(id="cpu_warn", interval_minutes=5, severity="WARNING",
             when=[Condition(fact="cpu.max", op=">=", value=80)]),
        Rule(id="cpu_crit", interval_minutes=5, severity="CRITICAL",
             when=[Condition(fact="cpu.max", op=">=", value=95)]),
        Rule(id="cpu_anomaly", interval_minutes=5, severity="WARNING", combine="AND",
             when=[Condition(fact="cpu.p95", op=">", value=80),
                   Condition(fact="cpu.spike_count", op=">", value=5)]),
        Rule(id="mem_high", interval_minutes=5, severity="WARNING",
             when=[Condition(fact="mem_used.max", op=">=", value=90)]),
        Rule(id="mem_low_free", interval_minutes=5, severity="WARNING",
             when=[Condition(fact="mem_free.min", op="<=", value=5)]),
        Rule(id="disk_full", interval_minutes=5, severity="CRITICAL",
             when=[Condition(fact="disk.max", op=">=", value=95, quantifier="any")]),
        Rule(id="temp_high", interval_minutes=5, severity="WARNING",
             when=[Condition(fact="temp.max", op=">=", value=90, quantifier="any")]),
        Rule(id="fan_low", interval_minutes=5, severity="WARNING",
             when=[Condition(fact="fan.min", op="<=", value=300, quantifier="any")]),
        Rule(id="volt_out", interval_minutes=5, severity="WARNING", combine="OR",
             when=[Condition(fact="volt_vcore.min", op="<", value=1.1),
                   Condition(fact="volt_vcore.max", op=">", value=1.4)]),
        Rule(id="gpu_hot", interval_minutes=5, severity="WARNING",
             when=[Condition(fact="gpu_load.p95", op=">=", value=95, quantifier="any")]),
        Rule(id="ssd_life_low", interval_minutes=5, severity="WARNING",
             when=[Condition(fact="ssd_life.min", op="<=", value=20, quantifier="any")]),
        Rule(id="disk_failing", interval_minutes=5, severity="CRITICAL",
             when=[Condition(fact="disk_health.max", op=">=", value=2, quantifier="any")]),
        Rule(id="proc_down", interval_minutes=5, severity="CRITICAL",
             when=[Condition(fact="proc_required.min", op="==", value=0, quantifier="any")]),
        Rule(id="proc_forbidden_run", interval_minutes=5, severity="CRITICAL",
             when=[Condition(fact="proc_forbidden.max", op=">", value=0, quantifier="any")]),
    ]
    return MonitorProfile(
        scope=Scope(process="*", eqp_model="*", eqp_id="*"),
        enabled=True,
        governance=Governance(updated_by="system", change_reason="initial seed"),
        measures=measures,
        rules=rules,
        notify={"default": NotifyChannel(cooldown_minutes=30, email_code="RESOURCE_MONITOR")},
    )


def _profile_hash(profile: MonitorProfile) -> str:
    """Deterministic hash of a profile's structural content (governance excluded
    so a fresh ``updated_at`` never forces a reseed)."""
    canonical = json.dumps(profile.structural_mongo(), sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


async def seed_default_profile(repo: ProfileRepository) -> None:
    """Ensure the global wildcard profile exists.

    Idempotent and safe for concurrent multi-replica startup:
    - absent → create the code default;
    - present & identical → skip;
    - present, drifted, but still code-owned (``governance.updated_by == "system"``)
      → reseed to propagate updated defaults;
    - present & operator-edited (updated_by != "system") → skip so operator
      changes to the global scope are never stomped.
    A concurrent insert race (two pods, fresh DB) surfaces ``DuplicateKeyError``
    on the unique scope index; the loser treats it as "already seeded".
    """
    default = build_default_profile()
    existing = await repo.find_by_scope(default.scope)
    if existing is not None:
        if _profile_hash(existing) == _profile_hash(default):
            logger.info("seed_profile_unchanged_skip")
            return
        if existing.governance.updated_by != "system":
            logger.info(
                "seed_profile_operator_edited_skip",
                updated_by=existing.governance.updated_by,
            )
            return
        logger.info("seed_profile_drifted_reseeding")
    else:
        logger.info("seed_profile_missing_creating")
    try:
        await repo.upsert(default)
    except DuplicateKeyError:
        # Lost a concurrent create race — the winner wrote an identical default.
        logger.info("seed_profile_lost_race_skip")
