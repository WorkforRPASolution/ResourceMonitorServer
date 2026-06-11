"""Integration: RmsEmailTemplateRepository against real Mongo.

Exercises the real motor find_one + projection + 5-tier '_' fallback boundary
that unit tests mock. The headline guarantee (architecture §7.1): a single
catch-all row serves arbitrary process/model — which Akka's getEmailBody cannot.
"""
import pytest

from src.config.constants import COLL_RMS_EMAIL_TEMPLATE
from src.db.repository import RmsEmailTemplateRepository

pytestmark = pytest.mark.integration


def _row(process, model, code, subcode, html):
    return {
        "app": "ARS", "process": process, "model": model,
        "code": code, "subcode": subcode, "title": "T", "html": html,
    }


async def test_catch_all_matches_arbitrary_process_model(fresh_mongo_db):
    coll = fresh_mongo_db[COLL_RMS_EMAIL_TEMPLATE]
    await coll.insert_one(_row("_", "_", "RESOURCE_MONITOR", "_", "CATCHALL"))
    repo = RmsEmailTemplateRepository(coll)

    doc = await repo.find_template(
        "ARS", "ANY_PROC", "ANY_MODEL", "RESOURCE_MONITOR", "CPU_CRITICAL"
    )
    assert doc is not None and doc["html"] == "CATCHALL"
    assert "_id" not in doc  # projection excludes _id


async def test_exact_beats_catch_all(fresh_mongo_db):
    coll = fresh_mongo_db[COLL_RMS_EMAIL_TEMPLATE]
    await coll.insert_many([
        _row("_", "_", "RESOURCE_MONITOR", "_", "CATCHALL"),
        _row("PHOTO", "MODEL_A", "RESOURCE_MONITOR", "CPU_CRITICAL", "EXACT"),
    ])
    repo = RmsEmailTemplateRepository(coll)

    doc = await repo.find_template(
        "ARS", "PHOTO", "MODEL_A", "RESOURCE_MONITOR", "CPU_CRITICAL"
    )
    assert doc["html"] == "EXACT"


async def test_miss_returns_none(fresh_mongo_db):
    repo = RmsEmailTemplateRepository(fresh_mongo_db[COLL_RMS_EMAIL_TEMPLATE])
    assert await repo.find_template(
        "ARS", "P", "M", "RESOURCE_MONITOR", "S"
    ) is None
