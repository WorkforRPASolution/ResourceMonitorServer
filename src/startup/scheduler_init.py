"""Build the analysis scheduler."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from src.config.settings import AppSettings
from src.scheduler.jobs import AnalysisScheduler


@dataclass
class SchedulerDeps:
    """Bag of everything the scheduler's jobs will need at run time.

    Phase 0 only stores references; Phase 1's analysis job will pull from
    these. Keeping it as a dataclass makes the dependencies explicit and
    test-friendly.
    """

    es: Any
    profile_repo: Any
    eqp_info_repo: Any
    zk_lock: Any
    cooldown_mgr: Any
    email_client: Any
    query_builder: Any
    # Option C: read-only RMS_EMAIL_TEMPLATE accessor. Default None so existing
    # constructions stay valid; only read when rms_custom_body_enabled.
    template_repo: Any = None


async def init_scheduler(
    settings: AppSettings, deps: SchedulerDeps
) -> AnalysisScheduler:
    return AnalysisScheduler(settings, deps)
