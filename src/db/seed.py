"""Seed default monitoring profile on startup.

Hash-based change detection: if the stored profile is byte-identical to the
built-in default, we leave it alone. This lets operators hand-edit the
profile without the service silently stomping their changes on restart.
"""
from __future__ import annotations

import hashlib
import json

import structlog

from src.db.models import (
    AnalysisConfig,
    MetricSchedule,
    MonitorProfile,
    Scope,
    ThresholdConfig,
)
from src.db.repository import ProfileRepository

logger = structlog.get_logger(__name__)


def build_default_profile() -> MonitorProfile:
    """The built-in global wildcard profile.

    This is intentionally conservative — Phase 0 only needs *a* profile so
    that the analyzer has something to iterate over. Real thresholds are
    tuned per-process in Phase 1+ via operator overrides.
    """
    return MonitorProfile(
        scope=Scope(process="*", eqp_model="*", eqp_id="*"),
        analysis_configs=[
            AnalysisConfig(
                metric_pattern="total_used_pct",
                threshold=ThresholdConfig(
                    warning=80.0, critical=95.0, cooldown_minutes=30
                ),
                schedule=MetricSchedule(
                    interval_minutes=5, window_minutes=10
                ),
            ),
            AnalysisConfig(
                metric_pattern="*_core_load",
                threshold=ThresholdConfig(
                    warning=85.0, critical=97.0, cooldown_minutes=30
                ),
                schedule=MetricSchedule(
                    interval_minutes=5, window_minutes=10
                ),
            ),
        ],
    )


def _profile_hash(profile: MonitorProfile) -> str:
    """Deterministic hash of a profile's semantically-relevant content.

    We hash `to_mongo()` so the comparison lives in the same representation
    that's actually stored. Keys are sorted to make the hash stable across
    dict ordering.
    """
    canonical = json.dumps(profile.to_mongo(), sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


async def seed_default_profile(repo: ProfileRepository) -> None:
    """Ensure the global wildcard profile exists; upsert only if it drifted."""
    default = build_default_profile()
    existing = await repo.find_by_scope(default.scope)
    if existing is not None:
        # Compare the *structural* content only — strip any id/timestamps.
        existing_for_hash = MonitorProfile(
            scope=existing.scope, analysis_configs=existing.analysis_configs
        )
        if _profile_hash(existing_for_hash) == _profile_hash(default):
            logger.info("seed_profile_unchanged_skip")
            return
        logger.info("seed_profile_drifted_reseeding")
    else:
        logger.info("seed_profile_missing_creating")
    await repo.upsert(default)
