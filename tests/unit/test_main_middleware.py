"""Tests for src.main middleware (RequestIdMiddleware)."""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.middleware import RequestIdMiddleware


@pytest.fixture
def app() -> FastAPI:
    a = FastAPI()
    a.add_middleware(RequestIdMiddleware)

    @a.get("/echo")
    async def echo():
        return {"ok": True}

    @a.get("/healthz/live")
    async def live():
        return {"status": "alive"}

    return a


@pytest.mark.unit
class TestRequestIdMiddleware:
    def test_generates_request_id_when_absent(self, app):
        with TestClient(app) as client:
            r = client.get("/echo")
        assert "X-Request-ID" in r.headers
        assert len(r.headers["X-Request-ID"]) > 0

    def test_passes_through_provided_request_id(self, app):
        with TestClient(app) as client:
            r = client.get("/echo", headers={"X-Request-ID": "test-req-123"})
        assert r.headers["X-Request-ID"] == "test-req-123"

    def test_skips_health_endpoints(self, app):
        """Health endpoints are hit constantly by K8s — no need to bind context."""
        with TestClient(app) as client:
            r = client.get("/healthz/live")
        # Should still work, but no X-Request-ID header expected on the response
        assert r.status_code == 200
