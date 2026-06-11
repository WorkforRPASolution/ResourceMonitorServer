"""Tests for RmsEmailTemplateRepository — read-only view of
RESOURCE_MONITOR_EMAIL_TEMPLATE with the 5-tier '_' wildcard fallback (§7.1).
"""
from unittest.mock import AsyncMock, MagicMock

import pytest
from pymongo.errors import ServerSelectionTimeoutError

from src.db.models import MongoUnavailableError
from src.db.repository import RmsEmailTemplateRepository

pytestmark = pytest.mark.unit


def _row(process, model, code, subcode, *, html="X", app="ARS"):
    return {
        "app": app, "process": process, "model": model,
        "code": code, "subcode": subcode, "title": "T", "html": html,
    }


def _coll(docs):
    """Mock collection whose find_one returns the first stored doc matching all
    query keys (mimicking an exact composite-key lookup)."""
    coll = MagicMock()

    async def find_one(query, projection=None):
        for d in docs:
            if all(d.get(k) == v for k, v in query.items()):
                return {k: v for k, v in d.items() if k != "_id"}
        return None

    coll.find_one = AsyncMock(side_effect=find_one)
    coll.find = MagicMock()
    coll.insert_one = AsyncMock()
    coll.update_one = AsyncMock()
    coll.replace_one = AsyncMock()
    coll.delete_one = AsyncMock()
    return coll


# (app, process, model, code, subcode) used by all lookups below
_KEY = ("ARS", "PHOTO", "MODEL_A", "RESOURCE_MONITOR", "CPU_CRITICAL")


class TestFallback:
    async def test_exact_match(self):
        repo = RmsEmailTemplateRepository(
            _coll([_row("PHOTO", "MODEL_A", "RESOURCE_MONITOR", "CPU_CRITICAL", html="EXACT")])
        )
        doc = await repo.find_template(*_KEY)
        assert doc is not None and doc["html"] == "EXACT"

    async def test_subcode_wildcard(self):
        repo = RmsEmailTemplateRepository(
            _coll([_row("PHOTO", "MODEL_A", "RESOURCE_MONITOR", "_", html="SUB")])
        )
        doc = await repo.find_template(*_KEY)
        assert doc["html"] == "SUB"

    async def test_model_wildcard(self):
        repo = RmsEmailTemplateRepository(
            _coll([_row("PHOTO", "_", "RESOURCE_MONITOR", "_", html="MODEL")])
        )
        doc = await repo.find_template(*_KEY)
        assert doc["html"] == "MODEL"

    async def test_process_wildcard(self):
        repo = RmsEmailTemplateRepository(
            _coll([_row("_", "_", "RESOURCE_MONITOR", "_", html="PROC")])
        )
        doc = await repo.find_template(*_KEY)
        assert doc["html"] == "PROC"

    async def test_code_wildcard_catch_all(self):
        repo = RmsEmailTemplateRepository(
            _coll([_row("_", "_", "_", "_", html="CATCHALL")])
        )
        doc = await repo.find_template(*_KEY)
        assert doc["html"] == "CATCHALL"

    async def test_all_miss_returns_none(self):
        repo = RmsEmailTemplateRepository(_coll([]))
        assert await repo.find_template(*_KEY) is None

    async def test_exact_precedence_over_catch_all(self):
        repo = RmsEmailTemplateRepository(_coll([
            _row("_", "_", "_", "_", html="CATCHALL"),
            _row("PHOTO", "MODEL_A", "RESOURCE_MONITOR", "CPU_CRITICAL", html="EXACT"),
        ]))
        doc = await repo.find_template(*_KEY)
        assert doc["html"] == "EXACT"


class TestReadOnly:
    async def test_accessor_issues_only_reads(self):
        coll = _coll([_row("_", "_", "_", "_")])
        repo = RmsEmailTemplateRepository(coll)
        await repo.find_template(*_KEY)
        coll.find_one.assert_awaited()
        coll.insert_one.assert_not_awaited()
        coll.update_one.assert_not_awaited()
        coll.replace_one.assert_not_awaited()
        coll.delete_one.assert_not_awaited()


class TestMongoUnavailable:
    async def test_translates_connection_error(self):
        coll = MagicMock()
        coll.find_one = AsyncMock(side_effect=ServerSelectionTimeoutError("down"))
        repo = RmsEmailTemplateRepository(coll)
        with pytest.raises(MongoUnavailableError):
            await repo.find_template(*_KEY)
