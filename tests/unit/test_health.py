"""Tests for src.api.health (split liveness/readiness)."""
import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api import health
from src.config.settings import AppSettings


@pytest.fixture
def app() -> FastAPI:
    a = FastAPI()
    a.include_router(health.router)
    return a


def _wire_state(
    app: FastAPI,
    *,
    es_ping=True,
    mongo_ping=True,
    redis_ping=True,
    zk_connected=True,
    email_health=True,
    scheduler_running=True,
    is_leader=False,
    debug_read_only=False,
    redistribute_unhealthy=False,
):
    app.state.settings = AppSettings(debug_read_only=debug_read_only)

    app.state.es_client = MagicMock()
    app.state.es_client.ping = AsyncMock(return_value=es_ping)

    app.state.mongo_client = MagicMock()
    app.state.mongo_client.ping = AsyncMock(return_value=mongo_ping)

    app.state.redis_client = MagicMock()
    app.state.redis_client.ping = AsyncMock(return_value=redis_ping)

    app.state.zk_client = MagicMock()
    app.state.zk_client.is_connected = MagicMock(return_value=zk_connected)

    app.state.email_client = MagicMock()
    app.state.email_client.health_check = AsyncMock(return_value=email_health)

    app.state.scheduler = MagicMock()
    app.state.scheduler.is_running = MagicMock(return_value=scheduler_running)

    app.state.partition_manager = MagicMock()
    app.state.partition_manager.is_leader = MagicMock(return_value=is_leader)
    # MagicMock.redistribute_unhealthy returns a Mock by default which is
    # truthy — set explicitly to satisfy the readiness check.
    app.state.partition_manager.redistribute_unhealthy = redistribute_unhealthy


@pytest.mark.unit
class TestLiveness:
    def test_liveness_always_200(self, app):
        # No state wired — liveness must NOT touch infra
        with TestClient(app) as client:
            r = client.get("/healthz/live")
        assert r.status_code == 200
        assert r.json()["status"] == "alive"


@pytest.mark.unit
class TestReadiness:
    def test_ready_when_all_checks_pass(self, app):
        _wire_state(app)
        with TestClient(app) as client:
            r = client.get("/healthz/ready")
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "ready"
        assert all(body["checks"].values())

    def test_503_when_es_down(self, app):
        _wire_state(app, es_ping=False)
        with TestClient(app) as client:
            r = client.get("/healthz/ready")
        assert r.status_code == 503
        assert r.json()["checks"]["elasticsearch"] is False

    def test_503_when_zk_disconnected(self, app):
        _wire_state(app, zk_connected=False)
        with TestClient(app) as client:
            r = client.get("/healthz/ready")
        assert r.status_code == 503
        assert r.json()["checks"]["zookeeper"] is False

    def test_503_when_mongo_down(self, app):
        _wire_state(app, mongo_ping=False)
        with TestClient(app) as client:
            r = client.get("/healthz/ready")
        assert r.status_code == 503

    def test_includes_scheduler_state_in_response(self, app):
        _wire_state(app, scheduler_running=True, is_leader=True)
        with TestClient(app) as client:
            r = client.get("/healthz/ready")
        body = r.json()
        assert body["scheduler_running"] is True
        assert body["is_leader"] is True

    def test_503_when_redistribute_unhealthy(self, app):
        """v6 P0-4: leader that exhausted redistribute retries surfaces
        as 503 even though every infra ping is green."""
        _wire_state(app, is_leader=True, redistribute_unhealthy=True)
        with TestClient(app) as client:
            r = client.get("/healthz/ready")
        assert r.status_code == 503
        body = r.json()
        assert body["status"] == "not_ready"
        assert body["redistribute_unhealthy"] is True
        # Sanity: every actual ping is still green
        assert all(v is True for k, v in body["checks"].items())

    def test_readiness_updates_infra_up_gauges(self, app):
        """v6 P0-5: every infra ping result must update INFRA_UP."""
        from src.api.metrics import INFRA_UP

        _wire_state(app, es_ping=False)  # ES down, others up
        with TestClient(app) as client:
            client.get("/healthz/ready")
        assert INFRA_UP.labels(infra="elasticsearch")._value.get() == 0.0
        assert INFRA_UP.labels(infra="mongodb")._value.get() == 1.0
        assert INFRA_UP.labels(infra="redis")._value.get() == 1.0
        assert INFRA_UP.labels(infra="email_api")._value.get() == 1.0
        assert INFRA_UP.labels(infra="zookeeper")._value.get() == 1.0

    def test_readiness_does_not_set_zk_gauge_in_debug_mode(self, app):
        """Debug pod is not in the cluster — leave zookeeper gauge alone
        so it stays at the prior (typically 0) value rather than reporting
        a fake 'up'."""
        from src.api.metrics import INFRA_UP

        # Pre-set ZK gauge to a sentinel
        INFRA_UP.labels(infra="zookeeper").set(42.0)

        _wire_state(app, debug_read_only=True)
        with TestClient(app) as client:
            client.get("/healthz/ready")
        # ZK gauge untouched
        assert INFRA_UP.labels(infra="zookeeper")._value.get() == 42.0
        # Cleanup
        INFRA_UP.labels(infra="zookeeper").set(0.0)


@pytest.mark.unit
class TestReadinessTimeoutGuard:
    def test_check_timeout_treated_as_failure(self, app):
        """A blocking ping must not stall the probe forever."""
        _wire_state(app)

        async def slow_ping():
            await asyncio.sleep(10)
            return True

        app.state.es_client.ping = slow_ping
        with TestClient(app) as client:
            r = client.get("/healthz/ready")
        # 503 because es ping timed out → treated as False
        assert r.status_code == 503
        assert r.json()["checks"]["elasticsearch"] is False
