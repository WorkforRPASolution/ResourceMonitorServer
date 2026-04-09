"""
============================================================
MOCKING RULES (STRICT):
- kazoo (KazooClient):       MagicMock  — synchronous API
- motor (AsyncIOMotorClient): AsyncMock
- redis.asyncio:              AsyncMock
- httpx.AsyncClient:          AsyncMock
- AsyncElasticsearch:         AsyncMock

FIXTURE NAMING: mock_<name>   (mock_es, mock_mongo, mock_redis, mock_zk, mock_email)
DATA fixtures:  sample_<name> (sample_profile, sample_scope)
============================================================
"""
from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.fixture(autouse=True)
def clear_settings_cache():
    """Ensure each test starts with a fresh AppSettings (env-sensitive)."""
    try:
        from src.config.settings import get_settings
    except ImportError:
        yield
        return
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@dataclass
class MockInfraContext:
    es: AsyncMock
    mongo: AsyncMock
    redis: AsyncMock
    zk: MagicMock
    email: AsyncMock


@pytest.fixture
def mock_infra() -> MockInfraContext:
    return MockInfraContext(
        es=AsyncMock(),
        mongo=AsyncMock(),
        redis=AsyncMock(),
        zk=MagicMock(),
        email=AsyncMock(),
    )


@pytest.fixture
def mock_es() -> AsyncMock:
    return AsyncMock()


@pytest.fixture
def mock_mongo() -> AsyncMock:
    return AsyncMock()


@pytest.fixture
def mock_redis() -> AsyncMock:
    return AsyncMock()


@pytest.fixture
def mock_zk() -> MagicMock:
    return MagicMock()


@pytest.fixture
def mock_email() -> AsyncMock:
    return AsyncMock()
