"""Tests for src.api.admin."""
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api import admin


@pytest.fixture
def app() -> FastAPI:
    a = FastAPI()
    a.include_router(admin.router)

    a.state.settings = MagicMock(instance_id="inst-1")

    pm = MagicMock()
    pm.is_leader = MagicMock(return_value=True)
    pm.get_my_processes = MagicMock(return_value=["CVD", "ETCH"])
    pm._leader = MagicMock(epoch=3)
    a.state.partition_manager = pm

    leader = MagicMock(epoch=3)
    a.state.leader_election = leader

    sched = MagicMock()
    sched.is_running = MagicMock(return_value=True)
    sched._scheduler = MagicMock()
    sched._scheduler.get_jobs = MagicMock(return_value=[])
    sched.reload = AsyncMock()
    sched.reconcile = AsyncMock(return_value=True)
    a.state.scheduler = sched

    zk = MagicMock()
    zk.is_connected = MagicMock(return_value=True)
    zk.get_server_version = AsyncMock(return_value="3.5.5")
    a.state.zk_client = zk

    cooldown = MagicMock()
    cooldown.clear_cooldown = AsyncMock()
    a.state.cooldown_manager = cooldown

    email = MagicMock()
    email.get_outbox_snapshot = MagicMock(return_value=[])
    email.outbox_max_size = 1000
    a.state.email_client = email
    return a


@pytest.mark.unit
class TestAdminStatus:
    def test_returns_instance_metadata(self, app):
        with TestClient(app) as client:
            r = client.get("/admin/status")
        assert r.status_code == 200
        body = r.json()
        assert body["instance_id"] == "inst-1"
        assert body["is_leader"] is True
        assert body["leader_epoch"] == 3
        assert body["assigned_processes"] == ["CVD", "ETCH"]
        assert body["zk_connected"] is True
        assert body["zk_server_version"] == "3.5.5"


@pytest.mark.unit
class TestAdminCooldownClear:
    def test_clear_cooldown(self, app):
        with TestClient(app) as client:
            r = client.delete(
                "/admin/cooldowns",
                params={
                    "process": "CVD", "eqp_id": "E1", "proc": "@system",
                    "notify": "default", "severity": "WARNING",
                },
            )
        assert r.status_code == 200
        assert r.json()["cleared"] == "CVD:E1:@system:default:WARNING"
        app.state.cooldown_manager.clear_cooldown.assert_awaited_once_with(
            "CVD", "E1", "@system", "default", "WARNING"
        )

    def test_clear_cooldown_proc_defaults_system(self, app):
        with TestClient(app) as client:
            r = client.delete(
                "/admin/cooldowns",
                params={
                    "process": "CVD", "eqp_id": "E1",
                    "notify": "default", "severity": "CRITICAL",
                },
            )
        assert r.status_code == 200
        app.state.cooldown_manager.clear_cooldown.assert_awaited_once_with(
            "CVD", "E1", "@system", "default", "CRITICAL"
        )


@pytest.mark.unit
class TestAdminSchedulerReload:
    def test_reload_triggers_reconcile(self, app):
        # The old reload() with no args was a no-op in prod (logged a warning
        # and returned). The endpoint now drives the cadence reconcile, which
        # works in both normal and debug mode.
        with TestClient(app) as client:
            r = client.post("/admin/scheduler/reload")
        assert r.status_code == 200
        assert r.json()["reconciled"] is True
        app.state.scheduler.reconcile.assert_awaited_once()
        app.state.scheduler.reload.assert_not_awaited()

    def test_reload_reports_no_change(self, app):
        app.state.scheduler.reconcile = AsyncMock(return_value=False)
        with TestClient(app) as client:
            r = client.post("/admin/scheduler/reload")
        assert r.status_code == 200
        assert r.json()["reconciled"] is False

    def test_reload_503_when_mongo_unavailable(self, app):
        # Mongo down during reconcile must surface as 503 (transient,
        # retryable) — consistent with the profile write API — not a 500.
        from src.db.models import MongoUnavailableError

        app.state.scheduler.reconcile = AsyncMock(
            side_effect=MongoUnavailableError("down")
        )
        with TestClient(app) as client:
            r = client.post("/admin/scheduler/reload")
        assert r.status_code == 503


@pytest.mark.unit
class TestAdminEmailOutbox:
    """v6 P1-3: snapshot of the in-memory failed-send outbox."""

    def test_returns_empty_outbox(self, app):
        with TestClient(app) as client:
            r = client.get("/admin/email-outbox")
        assert r.status_code == 200
        body = r.json()
        assert body["count"] == 0
        assert body["max_size"] == 1000
        assert body["entries"] == []
        assert body["available"] is True

    def test_returns_recent_entries(self, app):
        # 60 entries — endpoint should slice to last 50
        entries = [
            {"ts": float(i), "reason": "timeout", "detail": "", "payload": {"i": i}}
            for i in range(60)
        ]
        app.state.email_client.get_outbox_snapshot = MagicMock(return_value=entries)
        with TestClient(app) as client:
            r = client.get("/admin/email-outbox")
        assert r.status_code == 200
        body = r.json()
        assert body["count"] == 60
        assert len(body["entries"]) == 50
        # Newest 50 means entries 10..59
        assert body["entries"][0]["payload"]["i"] == 10
        assert body["entries"][-1]["payload"]["i"] == 59
        assert body["available"] is True

    def test_returns_unavailable_when_email_client_missing(self, app):
        """Debug-mode boots without an email client wired into app.state."""
        app.state.email_client = None
        with TestClient(app) as client:
            r = client.get("/admin/email-outbox")
        assert r.status_code == 200
        body = r.json()
        assert body["available"] is False
        assert body["count"] == 0
        assert body["entries"] == []
