"""Tests for src.db.client (MongoClient wrapper)."""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.config.settings import AppSettings
from src.db.client import MongoClient


@pytest.fixture
def settings() -> AppSettings:
    return AppSettings(mongo_uri="mongodb://localhost:27017", mongo_db="EARS")


@pytest.mark.unit
class TestMongoClientConnect:
    async def test_connect_calls_ping_on_admin_db(self, settings):
        client = MongoClient(settings)
        with patch("src.db.client.AsyncIOMotorClient") as mock_cls:
            instance = MagicMock()
            instance.admin.command = AsyncMock(return_value={"ok": 1})
            # __getitem__ returns a db object
            instance.__getitem__.return_value = MagicMock()
            mock_cls.return_value = instance
            await client.connect_with_retry(max_attempts=1, backoff=0.0)
        instance.admin.command.assert_awaited_once_with("ping")

    async def test_connect_retries_on_failure(self, settings):
        client = MongoClient(settings)
        call_count = {"n": 0}

        def fake_client(*_a, **_kw):
            call_count["n"] += 1
            instance = MagicMock()
            if call_count["n"] < 3:
                instance.admin.command = AsyncMock(
                    side_effect=ConnectionError("boom")
                )
            else:
                instance.admin.command = AsyncMock(return_value={"ok": 1})
            instance.__getitem__.return_value = MagicMock()
            return instance

        with patch("src.db.client.AsyncIOMotorClient", side_effect=fake_client):
            await client.connect_with_retry(max_attempts=5, backoff=0.0)
        assert call_count["n"] == 3

    async def test_connect_raises_after_max_attempts(self, settings):
        client = MongoClient(settings)
        with patch("src.db.client.AsyncIOMotorClient") as mock_cls:
            instance = MagicMock()
            instance.admin.command = AsyncMock(
                side_effect=ConnectionError("boom")
            )
            instance.__getitem__.return_value = MagicMock()
            mock_cls.return_value = instance
            with pytest.raises(ConnectionError):
                await client.connect_with_retry(max_attempts=2, backoff=0.0)


@pytest.mark.unit
class TestMongoClientClose:
    async def test_close_is_sync_call(self, settings):
        """motor's AsyncIOMotorClient.close() is SYNCHRONOUS — must not be awaited."""
        client = MongoClient(settings)
        underlying = MagicMock()  # NOT AsyncMock: close() is sync
        client._client = underlying
        await client.close()
        underlying.close.assert_called_once()  # sync call, no .assert_awaited

    async def test_close_noop_when_not_connected(self, settings):
        client = MongoClient(settings)
        await client.close()  # must not raise


@pytest.mark.unit
class TestMongoClientPing:
    async def test_ping_returns_true_on_success(self, settings):
        client = MongoClient(settings)
        client._client = MagicMock()
        client._client.admin.command = AsyncMock(return_value={"ok": 1})
        assert await client.ping() is True

    async def test_ping_returns_false_on_failure(self, settings):
        client = MongoClient(settings)
        client._client = MagicMock()
        client._client.admin.command = AsyncMock(
            side_effect=ConnectionError("boom")
        )
        assert await client.ping() is False

    async def test_ping_returns_false_when_not_connected(self, settings):
        client = MongoClient(settings)
        assert await client.ping() is False
