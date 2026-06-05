"""Tests for src.api.profiles — v2 profile CRUD API."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api import profiles
from src.db.models import (
    Condition,
    Fact,
    Measure,
    MonitorProfile,
    NotifyChannel,
    ProfileAlreadyExistsError,
    ProfileVersionConflictError,
    Rule,
    Scope,
)

pytestmark = pytest.mark.unit


def _overlay(version=1, scope=None, measures=None, rules=None):
    return MonitorProfile(
        scope=scope or Scope(process="*"),
        governance={"version": version},
        measures=measures if measures is not None else [
            Measure(id="cpu", category="cpu", metric="total_used_pct",
                    window_minutes=15, facts=[Fact(type="max")])
        ],
        rules=rules if rules is not None else [
            Rule(id="cpu_warn", interval_minutes=5, severity="WARNING",
                 when=[Condition(fact="cpu.max", op=">=", value=80)])
        ],
        notify={"default": NotifyChannel(cooldown_minutes=30)},
    )


@pytest.fixture
def repo() -> MagicMock:
    r = MagicMock()
    r.find_by_scope = AsyncMock(return_value=None)
    r.collect_scope_docs = AsyncMock(return_value=[])
    r.create = AsyncMock(return_value="id123")
    r.replace_with_version = AsyncMock(return_value=2)
    r.delete_by_scope = AsyncMock()
    return r


@pytest.fixture
def client(repo) -> TestClient:
    app = FastAPI()
    app.include_router(profiles.router)
    app.state.repos = SimpleNamespace(profile_repo=repo)
    return TestClient(app)


# scope query for the global profile
_GS = {"process": "*"}


class TestRead:
    def test_get_overlay_200(self, client, repo):
        repo.find_by_scope.return_value = _overlay()
        r = client.get("/profiles", params=_GS)
        assert r.status_code == 200
        assert [m["id"] for m in r.json()["measures"]] == ["cpu"]

    def test_get_overlay_404(self, client, repo):
        repo.find_by_scope.return_value = None
        assert client.get("/profiles", params=_GS).status_code == 404

    def test_get_effective_200(self, client, repo):
        repo.collect_scope_docs.return_value = [_overlay()]
        r = client.get("/profiles/effective", params=_GS)
        assert r.status_code == 200
        assert [rule["id"] for rule in r.json()["rules"]] == ["cpu_warn"]

    def test_get_effective_404_when_no_docs(self, client, repo):
        repo.collect_scope_docs.return_value = []
        assert client.get("/profiles/effective", params=_GS).status_code == 404

    def test_get_effective_503_when_db_unavailable(self, client, repo):
        from src.db.models import MongoUnavailableError
        repo.collect_scope_docs.side_effect = MongoUnavailableError("down")
        assert client.get("/profiles/effective", params=_GS).status_code == 503

    def test_get_effective_with_provenance(self, client, repo):
        glob = _overlay(scope=Scope(process="*"))
        overlay = MonitorProfile(
            scope=Scope(process="CVD", eqp_model="M", eqp_id="E1"),
            rules=[Rule(id="cpu_crit", interval_minutes=5, severity="CRITICAL",
                        when=[Condition(fact="cpu.max", op=">=", value=95)])],
        )
        repo.collect_scope_docs.return_value = [glob, overlay]
        r = client.get("/profiles/effective",
                       params={"process": "CVD", "model": "M", "eqpId": "E1",
                               "withProvenance": "true"})
        body = r.json()
        assert r.status_code == 200
        assert body["provenance"]["rules"]["cpu_crit"] == "CVD/M/E1"
        assert body["provenance"]["rules"]["cpu_warn"] == "*/*/*"


class TestCreateReplaceDelete:
    def test_create_201(self, client, repo):
        body = {
            "scope": {"process": "CVD"},
            "measures": [{"id": "cpu", "category": "cpu", "metric": "total_used_pct",
                          "window_minutes": 15, "facts": [{"type": "max"}]}],
            "rules": [{"id": "w", "interval_minutes": 5, "severity": "WARNING",
                       "when": [{"fact": "cpu.max", "op": ">=", "value": 80}]}],
            "notify": {"default": {"cooldown_minutes": 30}},
        }
        r = client.post("/profiles", json=body)
        assert r.status_code == 201
        assert r.json()["id"] == "id123"
        repo.create.assert_awaited_once()

    def test_create_422_on_dangling_reference(self, client, repo):
        body = {
            "scope": {"process": "CVD"},
            "measures": [],
            "rules": [{"id": "w", "interval_minutes": 5, "severity": "WARNING",
                       "when": [{"fact": "ghost.max", "op": ">=", "value": 80}]}],
            "notify": {"default": {"cooldown_minutes": 30}},
        }
        r = client.post("/profiles", json=body)
        assert r.status_code == 422
        repo.create.assert_not_awaited()

    def test_create_503_when_db_unavailable(self, client, repo):
        from src.db.models import MongoUnavailableError
        repo.collect_scope_docs.side_effect = MongoUnavailableError("down")
        body = {
            "scope": {"process": "CVD"},
            "measures": [{"id": "cpu", "category": "cpu", "metric": "x",
                          "window_minutes": 15, "facts": [{"type": "max"}]}],
            "rules": [{"id": "w", "interval_minutes": 5, "severity": "WARNING",
                       "when": [{"fact": "cpu.max", "op": ">=", "value": 80}]}],
            "notify": {"default": {"cooldown_minutes": 30}},
        }
        assert client.post("/profiles", json=body).status_code == 503

    def test_create_409_when_exists(self, client, repo):
        repo.create.side_effect = ProfileAlreadyExistsError(Scope(process="CVD"))
        body = {
            "scope": {"process": "CVD"},
            "measures": [{"id": "cpu", "category": "cpu", "metric": "x",
                          "window_minutes": 15, "facts": [{"type": "max"}]}],
            "rules": [{"id": "w", "interval_minutes": 5, "severity": "WARNING",
                       "when": [{"fact": "cpu.max", "op": ">=", "value": 80}]}],
            "notify": {"default": {"cooldown_minutes": 30}},
        }
        assert client.post("/profiles", json=body).status_code == 409

    def test_replace_returns_new_version(self, client, repo):
        repo.replace_with_version.return_value = 5
        body = {
            "scope": {"process": "CVD"}, "expected_version": 4,
            "measures": [{"id": "cpu", "category": "cpu", "metric": "x",
                          "window_minutes": 15, "facts": [{"type": "max"}]}],
            "rules": [{"id": "w", "interval_minutes": 5, "severity": "WARNING",
                       "when": [{"fact": "cpu.max", "op": ">=", "value": 80}]}],
            "notify": {"default": {"cooldown_minutes": 30}},
        }
        repo.collect_scope_docs.return_value = []
        r = client.put("/profiles", json=body)
        assert r.status_code == 200 and r.json()["version"] == 5

    def test_replace_409_on_stale_version(self, client, repo):
        repo.replace_with_version.side_effect = ProfileVersionConflictError(
            Scope(process="CVD"), 4)
        body = {
            "scope": {"process": "CVD"}, "expected_version": 4,
            "measures": [{"id": "cpu", "category": "cpu", "metric": "x",
                          "window_minutes": 15, "facts": [{"type": "max"}]}],
            "rules": [{"id": "w", "interval_minutes": 5, "severity": "WARNING",
                       "when": [{"fact": "cpu.max", "op": ">=", "value": 80}]}],
            "notify": {"default": {"cooldown_minutes": 30}},
        }
        assert client.put("/profiles", json=body).status_code == 409

    def test_delete_overlay(self, client, repo):
        r = client.delete("/profiles", params={"process": "CVD", "version": 3})
        assert r.status_code == 200
        repo.delete_by_scope.assert_awaited_once()


class TestItemCrud:
    def test_add_measure(self, client, repo):
        overlay = _overlay()
        repo.find_by_scope.return_value = overlay
        repo.collect_scope_docs.return_value = [_overlay()]
        body = {
            "scope": {"process": "*"}, "expected_version": 1,
            "measure": {"id": "mem", "category": "memory", "metric": "total_used_pct",
                        "window_minutes": 15, "facts": [{"type": "max"}]},
        }
        r = client.post("/profiles/measures", json=body)
        assert r.status_code == 200
        repo.replace_with_version.assert_awaited_once()

    def test_add_measure_duplicate_409(self, client, repo):
        repo.find_by_scope.return_value = _overlay()
        body = {
            "scope": {"process": "*"}, "expected_version": 1,
            "measure": {"id": "cpu", "category": "cpu", "metric": "x",
                        "window_minutes": 15, "facts": [{"type": "max"}]},
        }
        assert client.post("/profiles/measures", json=body).status_code == 409

    def test_update_measure_404(self, client, repo):
        repo.find_by_scope.return_value = _overlay()
        body = {
            "scope": {"process": "*"}, "expected_version": 1,
            "measure": {"id": "ghost", "category": "cpu", "metric": "x",
                        "window_minutes": 15, "facts": [{"type": "max"}]},
        }
        assert client.patch("/profiles/measures/ghost", json=body).status_code == 404

    def test_delete_measure_dangling_422(self, client, repo):
        # overlay has cpu measure + cpu_warn rule referencing cpu.max
        overlay = _overlay()
        repo.find_by_scope.return_value = overlay
        repo.collect_scope_docs.return_value = [_overlay()]
        r = client.delete("/profiles/measures/cpu",
                          params={"process": "*", "version": 1})
        assert r.status_code == 422  # cpu_warn now references a missing measure
        repo.replace_with_version.assert_not_awaited()

    def test_add_rule(self, client, repo):
        repo.find_by_scope.return_value = _overlay()
        repo.collect_scope_docs.return_value = [_overlay()]
        body = {
            "scope": {"process": "*"}, "expected_version": 1,
            "rule": {"id": "cpu_crit", "interval_minutes": 5, "severity": "CRITICAL",
                     "when": [{"fact": "cpu.max", "op": ">=", "value": 95}]},
        }
        r = client.post("/profiles/rules", json=body)
        assert r.status_code == 200

    def test_patch_notify(self, client, repo):
        repo.find_by_scope.return_value = _overlay()
        repo.collect_scope_docs.return_value = [_overlay()]
        body = {
            "scope": {"process": "*"}, "expected_version": 1,
            "channel": {"cooldown_minutes": 60, "email_subcode": "PAGER"},
        }
        r = client.patch("/profiles/notify/default", json=body)
        assert r.status_code == 200
        repo.replace_with_version.assert_awaited_once()

    def test_item_write_409_on_stale_version(self, client, repo):
        repo.find_by_scope.return_value = _overlay()
        repo.collect_scope_docs.return_value = [_overlay()]
        repo.replace_with_version.side_effect = ProfileVersionConflictError(
            Scope(process="*"), 1)
        body = {
            "scope": {"process": "*"}, "expected_version": 1,
            "rule": {"id": "cpu_crit", "interval_minutes": 5, "severity": "CRITICAL",
                     "when": [{"fact": "cpu.max", "op": ">=", "value": 95}]},
        }
        assert client.post("/profiles/rules", json=body).status_code == 409
