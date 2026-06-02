"""Tests for src.analyzer.engine — the Phase 1 analysis orchestration."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.db.models import AnalysisConfig, MetricSchedule, ThresholdConfig

pytestmark = pytest.mark.unit


def _make_config(
    pattern: str = "total_used_pct",
    warning: float = 80.0,
    critical: float = 95.0,
    cooldown: int = 30,
    interval: int = 5,
    window: int = 10,
) -> AnalysisConfig:
    return AnalysisConfig(
        metric_pattern=pattern,
        threshold=ThresholdConfig(
            warning=warning, critical=critical, cooldown_minutes=cooldown
        ),
        schedule=MetricSchedule(
            interval_minutes=interval, window_minutes=window
        ),
    )


def _make_deps():
    """Build a mock SchedulerDeps bag."""
    deps = MagicMock()
    deps.es = MagicMock()
    deps.es.client = AsyncMock()
    deps.es.get_numeric_field_names = AsyncMock(return_value=["total_used_pct"])
    deps.profile_repo = AsyncMock()
    deps.eqp_info_repo = AsyncMock()
    deps.eqp_info_repo.get_active_equipment_by_process = AsyncMock(return_value=[
        {"eqpId": "EQP01", "eqpModel": "MODEL_A", "process": "CVD",
         "localpc": "PC001", "ipAddr": "10.0.0.1", "line": "L1", "category": "MAIN"},
    ])
    deps.zk_lock = MagicMock()
    deps.zk_lock.acquire = MagicMock(return_value=AsyncMock(
        __aenter__=AsyncMock(return_value=None),
        __aexit__=AsyncMock(return_value=False),
    ))
    deps.cooldown_mgr = AsyncMock()
    deps.cooldown_mgr.is_cooling_down_batch = AsyncMock(return_value={})
    deps.email_client = AsyncMock()
    deps.email_client.send_alert = AsyncMock(return_value=True)
    deps.query_builder = MagicMock()
    deps.query_builder.resolve_index_range = MagicMock(return_value="cvd_all-2026.04.10")
    deps.query_builder.build_metric_aggregation_query = MagicMock(return_value={"size": 0})
    return deps


def _make_settings():
    settings = MagicMock()
    settings.debug_read_only = False
    settings.grafana_base_url = "http://grafana:3000"
    settings.grafana_dashboard_uid = "abc123"
    settings.email_app_name = "ARS"
    return settings


def _es_response(eqp_id: str, field: str, value: float, agg_type: str = "max"):
    """Build a minimal ES aggregation response."""
    return {
        "aggregations": {
            "by_eqp": {
                "buckets": [
                    {
                        "key": eqp_id,
                        "doc_count": 10,
                        f"{field}_{agg_type}": {"value": value},
                    }
                ]
            }
        }
    }


class TestAnalysisEngineHappyPath:
    async def test_full_flow_breach_sends_email(self):
        from src.analyzer.engine import AnalysisEngine

        deps = _make_deps()
        settings = _make_settings()
        # ES returns value above warning threshold
        deps.es.client.search = AsyncMock(
            return_value=_es_response("EQP01", "total_used_pct", 92.5)
        )
        # Not cooling down
        deps.cooldown_mgr.is_cooling_down_batch.return_value = {
            ("EQP01", "CPU", "total_used_pct"): False,
        }

        import asyncio
        engine = AnalysisEngine(deps, settings)
        engine._es_semaphore = asyncio.Semaphore(3)
        config = _make_config(warning=80.0, critical=95.0)
        result = await engine.run_analysis("CVD", config)

        # Email sent
        deps.email_client.send_alert.assert_awaited_once()
        # Cooldown set
        deps.cooldown_mgr.set_cooldown.assert_awaited_once()
        # Result has breaches
        assert len(result.breaches) == 1
        assert result.breaches[0].severity == "WARNING"

    async def test_no_breach_no_email(self):
        from src.analyzer.engine import AnalysisEngine

        deps = _make_deps()
        settings = _make_settings()
        # ES returns value below warning
        deps.es.client.search = AsyncMock(
            return_value=_es_response("EQP01", "total_used_pct", 50.0)
        )

        import asyncio
        engine = AnalysisEngine(deps, settings)
        engine._es_semaphore = asyncio.Semaphore(3)
        config = _make_config(warning=80.0, critical=95.0)
        result = await engine.run_analysis("CVD", config)

        deps.email_client.send_alert.assert_not_awaited()
        assert len(result.breaches) == 0


class TestAnalysisEngineCooldown:
    async def test_cooldown_suppresses_email(self):
        from src.analyzer.engine import AnalysisEngine

        deps = _make_deps()
        settings = _make_settings()
        deps.es.client.search = AsyncMock(
            return_value=_es_response("EQP01", "total_used_pct", 92.5)
        )
        # Already cooling down
        deps.cooldown_mgr.is_cooling_down_batch.return_value = {
            ("EQP01", "CPU", "total_used_pct"): True,
        }

        import asyncio
        engine = AnalysisEngine(deps, settings)
        engine._es_semaphore = asyncio.Semaphore(3)
        result = await engine.run_analysis("CVD", _make_config())

        # Breach detected but email not sent
        assert len(result.breaches) == 1
        deps.email_client.send_alert.assert_not_awaited()
        deps.cooldown_mgr.set_cooldown.assert_not_awaited()

    async def test_send_failure_does_not_set_cooldown(self):
        from src.analyzer.engine import AnalysisEngine

        deps = _make_deps()
        settings = _make_settings()
        deps.es.client.search = AsyncMock(
            return_value=_es_response("EQP01", "total_used_pct", 92.5)
        )
        deps.cooldown_mgr.is_cooling_down_batch.return_value = {
            ("EQP01", "CPU", "total_used_pct"): False,
        }
        deps.email_client.send_alert = AsyncMock(return_value=False)

        import asyncio
        engine = AnalysisEngine(deps, settings)
        engine._es_semaphore = asyncio.Semaphore(3)
        await engine.run_analysis("CVD", _make_config())

        # Email attempted but failed — cooldown must NOT be set
        deps.email_client.send_alert.assert_awaited_once()
        deps.cooldown_mgr.set_cooldown.assert_not_awaited()


class TestAnalysisEngineEdgeCases:
    async def test_no_matching_fields_returns_early(self):
        from src.analyzer.engine import AnalysisEngine

        deps = _make_deps()
        settings = _make_settings()
        deps.es.get_numeric_field_names.return_value = ["unrelated_field"]

        import asyncio
        engine = AnalysisEngine(deps, settings)
        engine._es_semaphore = asyncio.Semaphore(3)
        config = _make_config(pattern="total_used_pct")
        result = await engine.run_analysis("CVD", config)

        # No ES query executed
        deps.es.client.search.assert_not_awaited()
        assert len(result.breaches) == 0

    async def test_eqp_not_in_lookup_skipped(self):
        from src.analyzer.engine import AnalysisEngine

        deps = _make_deps()
        settings = _make_settings()
        # ES returns data for EQP99 which is not in the equipment list
        deps.es.client.search = AsyncMock(
            return_value=_es_response("EQP99", "total_used_pct", 92.5)
        )
        deps.cooldown_mgr.is_cooling_down_batch.return_value = {
            ("EQP99", "CPU", "total_used_pct"): False,
        }

        import asyncio
        engine = AnalysisEngine(deps, settings)
        engine._es_semaphore = asyncio.Semaphore(3)
        result = await engine.run_analysis("CVD", _make_config())

        # Breach detected but equipment not in lookup — email skipped
        assert len(result.breaches) == 1
        deps.email_client.send_alert.assert_not_awaited()

    async def test_zk_lock_is_acquired(self):
        from src.analyzer.engine import AnalysisEngine

        deps = _make_deps()
        settings = _make_settings()
        deps.es.client.search = AsyncMock(
            return_value=_es_response("EQP01", "total_used_pct", 50.0)
        )

        import asyncio
        engine = AnalysisEngine(deps, settings)
        engine._es_semaphore = asyncio.Semaphore(3)
        await engine.run_analysis("CVD", _make_config())

        deps.zk_lock.acquire.assert_called_once_with("CVD")
