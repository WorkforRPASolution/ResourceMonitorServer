"""Tests for src.es.client (ES 7.11.9 API compat)."""
from unittest.mock import AsyncMock, patch

import pytest
import structlog
from elasticsearch.exceptions import NotFoundError

from src.config.settings import AppSettings
from src.es.client import ESClient


class _ManualClock:
    """Drop-in replacement for ``time.monotonic`` whose value is advanced
    explicitly by tests. cachetools imports ``monotonic`` directly via
    ``from time import monotonic`` so ``time-machine`` cannot patch it;
    we inject this into ``ESClient`` instead."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def settings() -> AppSettings:
    return AppSettings(
        es_hosts=["http://es:9200"],
        es_username="elastic",
        es_password="changeme",
        es_request_timeout=15,
        es_max_retries=2,
    )


def _mock_es_instance(ping_result: bool = True) -> AsyncMock:
    """AsyncMock with ping() returning ``ping_result``.

    Used by every ``connect()`` test because v6 P0-3 added a ping at the
    end of connect, so the mocked AsyncElasticsearch needs an awaitable
    ping or it will raise a TypeError on the wrapper's ``self.ping()``
    call. Default True keeps existing tests semantically unchanged.
    """
    instance = AsyncMock()
    instance.ping.return_value = ping_result
    return instance


@pytest.mark.unit
class TestESClientConnect:
    async def test_connect_uses_http_auth_for_es_7x(self, settings):
        """ES 7.x client takes `http_auth`, NOT `basic_auth` (8.x)."""
        client = ESClient(settings)
        with patch("src.es.client.AsyncElasticsearch") as mock_cls:
            mock_cls.return_value = _mock_es_instance()
            await client.connect()
        kwargs = mock_cls.call_args.kwargs
        assert "http_auth" in kwargs
        assert kwargs["http_auth"] == ("elastic", "changeme")
        assert "basic_auth" not in kwargs

    async def test_connect_uses_timeout_not_request_timeout(self, settings):
        """ES 7.x takes `timeout`, NOT `request_timeout` (8.x)."""
        client = ESClient(settings)
        with patch("src.es.client.AsyncElasticsearch") as mock_cls:
            mock_cls.return_value = _mock_es_instance()
            await client.connect()
        kwargs = mock_cls.call_args.kwargs
        assert kwargs["timeout"] == 15
        assert "request_timeout" not in kwargs

    async def test_connect_passes_hosts_list(self, settings):
        client = ESClient(settings)
        with patch("src.es.client.AsyncElasticsearch") as mock_cls:
            mock_cls.return_value = _mock_es_instance()
            await client.connect()
        kwargs = mock_cls.call_args.kwargs
        assert kwargs["hosts"] == ["http://es:9200"]

    async def test_connect_without_username_omits_auth(self):
        settings = AppSettings(es_hosts=["http://es:9200"], es_username="")
        client = ESClient(settings)
        with patch("src.es.client.AsyncElasticsearch") as mock_cls:
            mock_cls.return_value = _mock_es_instance()
            await client.connect()
        kwargs = mock_cls.call_args.kwargs
        assert "http_auth" not in kwargs

    async def test_connect_with_ssl_enables_verify_certs(self):
        settings = AppSettings(es_hosts=["https://es:9200"], es_use_ssl=True)
        client = ESClient(settings)
        with patch("src.es.client.AsyncElasticsearch") as mock_cls:
            mock_cls.return_value = _mock_es_instance()
            await client.connect()
        kwargs = mock_cls.call_args.kwargs
        assert kwargs.get("verify_certs") is True
        assert kwargs.get("use_ssl") is True

    async def test_connect_pings_at_end(self, settings):
        """v6 P0-3: connect() must ping the cluster, not just instantiate
        the client. Without this, a typo in MONITOR_ES_HOSTS boots cleanly
        and only fails at first query."""
        client = ESClient(settings)
        with patch("src.es.client.AsyncElasticsearch") as mock_cls:
            instance = _mock_es_instance(ping_result=True)
            mock_cls.return_value = instance
            await client.connect()
        instance.ping.assert_awaited_once()

    async def test_connect_raises_when_ping_returns_false(self, settings):
        client = ESClient(settings)
        with patch("src.es.client.AsyncElasticsearch") as mock_cls:
            mock_cls.return_value = _mock_es_instance(ping_result=False)
            with pytest.raises(RuntimeError, match="es_startup_ping_failed"):
                await client.connect()

    async def test_connect_raises_when_ping_throws(self, settings):
        client = ESClient(settings)
        with patch("src.es.client.AsyncElasticsearch") as mock_cls:
            instance = AsyncMock()
            instance.ping.side_effect = ConnectionError("boom")
            mock_cls.return_value = instance
            # ESClient.ping() catches exceptions and returns False, so the
            # connect()-raised error is the same RuntimeError.
            with pytest.raises(RuntimeError, match="es_startup_ping_failed"):
                await client.connect()

    async def test_connect_failure_logs_hosts(self, settings):
        """기동 실패 시 어느 ES 인스턴스/설정 문제인지 즉시 식별 가능해야 한다
        (fail-fast 진단) — es_connect_failed 이벤트가 hosts 를 담는다."""
        client = ESClient(settings)
        with patch("src.es.client.AsyncElasticsearch") as mock_cls:
            mock_cls.return_value = _mock_es_instance(ping_result=False)
            with structlog.testing.capture_logs() as cap:
                with pytest.raises(RuntimeError):
                    await client.connect()
        evts = [e for e in cap if e["event"] == "es_connect_failed"]
        assert evts, "es_connect_failed not logged"
        assert evts[0]["log_level"] == "error"
        assert evts[0]["hosts"] == settings.es_hosts


@pytest.mark.unit
class TestESClientPing:
    async def test_ping_delegates_to_underlying(self, settings):
        client = ESClient(settings)
        client._client = AsyncMock()
        client._client.ping.return_value = True
        assert await client.ping() is True

    async def test_ping_returns_false_on_exception(self, settings):
        client = ESClient(settings)
        client._client = AsyncMock()
        client._client.ping.side_effect = ConnectionError("boom")
        assert await client.ping() is False

    async def test_ping_returns_false_when_not_connected(self, settings):
        client = ESClient(settings)
        assert await client.ping() is False


@pytest.mark.unit
class TestESClientClose:
    async def test_close_calls_underlying_close(self, settings):
        client = ESClient(settings)
        underlying = AsyncMock()
        client._client = underlying
        await client.close()
        underlying.close.assert_awaited_once()
        assert client._client is None  # reset defensively

    async def test_close_noop_when_not_connected(self, settings):
        client = ESClient(settings)
        await client.close()  # must not raise


@pytest.mark.unit
class TestIntrospectFieldType:
    async def test_returns_cached_type_on_second_call(self, settings):
        client = ESClient(settings)
        client._client = AsyncMock()
        client._client.indices.get_mapping.return_value = {
            "cvd_all-2026.04.07": {
                "mappings": {"properties": {"category": {"type": "keyword"}}}
            }
        }
        t1 = await client.introspect_field_type("cvd_all-*", "category")
        t2 = await client.introspect_field_type("cvd_all-*", "category")
        assert t1 == "keyword"
        assert t2 == "keyword"
        # Only one underlying call (cached)
        assert client._client.indices.get_mapping.call_count == 1

    async def test_returns_unknown_on_not_found(self, settings):
        client = ESClient(settings)
        client._client = AsyncMock()
        client._client.indices.get_mapping.side_effect = NotFoundError(
            "no such index", {}, {}
        )
        t = await client.introspect_field_type("cvd_all-*", "category")
        assert t == "unknown"

    async def test_does_not_retry_within_negative_ttl(self, settings):
        """Within the 5-min negative TTL, subsequent calls return cached
        'unknown' without re-hitting ES."""
        client = ESClient(settings)
        client._client = AsyncMock()
        client._client.indices.get_mapping.side_effect = NotFoundError(
            "no such index", {}, {}
        )
        await client.introspect_field_type("cvd_all-*", "category")
        await client.introspect_field_type("cvd_all-*", "category")
        assert client._client.indices.get_mapping.call_count == 1

    async def test_retries_after_negative_ttl_expires(self, settings):
        """v6 P1-5: an index that did not exist at boot must be re-checked
        once the negative TTL (5 min) elapses, so a nightly index roll is
        picked up without a pod restart.
        """
        clock = _ManualClock()
        client = ESClient(settings, introspect_timer=clock)
        client._client = AsyncMock()

        # First call: missing → cached as "unknown" for 5 min
        client._client.indices.get_mapping.side_effect = NotFoundError(
            "no such index", {}, {}
        )
        t1 = await client.introspect_field_type("cvd_all-*", "category")
        assert t1 == "unknown"

        # 4 min later — still cached, no re-call
        clock.advance(240)
        t2 = await client.introspect_field_type("cvd_all-*", "category")
        assert t2 == "unknown"
        assert client._client.indices.get_mapping.call_count == 1

        # 6 min after the original call — negative TTL (5 min) has expired,
        # should hit ES again. This time the index exists.
        clock.advance(120)  # cumulative +6 min
        client._client.indices.get_mapping.side_effect = None
        client._client.indices.get_mapping.return_value = {
            "cvd_all-2026.04.08": {
                "mappings": {"properties": {"category": {"type": "keyword"}}}
            }
        }
        t3 = await client.introspect_field_type("cvd_all-*", "category")
        assert t3 == "keyword"
        assert client._client.indices.get_mapping.call_count == 2

    async def test_positive_cache_lives_at_least_negative_ttl(self, settings):
        """Sanity guard: the positive TTL must outlive the negative TTL,
        otherwise the negative TTL change has no value (a successful
        introspect would re-fire just as often as a failed one).
        """
        clock = _ManualClock()
        client = ESClient(settings, introspect_timer=clock)
        client._client = AsyncMock()
        client._client.indices.get_mapping.return_value = {
            "cvd_all-2026.04.08": {
                "mappings": {"properties": {"category": {"type": "keyword"}}}
            }
        }
        await client.introspect_field_type("cvd_all-*", "category")
        # 6 min later — past the negative TTL (5 min) but well within the
        # positive TTL (10 min). Must still be cached.
        clock.advance(360)
        await client.introspect_field_type("cvd_all-*", "category")
        assert client._client.indices.get_mapping.call_count == 1

    async def test_returns_unknown_when_field_not_in_mapping(self, settings):
        client = ESClient(settings)
        client._client = AsyncMock()
        client._client.indices.get_mapping.return_value = {
            "cvd_all-2026.04.07": {"mappings": {"properties": {}}}
        }
        t = await client.introspect_field_type("cvd_all-*", "category")
        assert t == "unknown"


# ----------------------------------------------------------------------
# v2: get_metric_names (distinct EARS_METRIC via terms agg)
# ----------------------------------------------------------------------
@pytest.mark.unit
class TestGetMetricNames:
    @staticmethod
    def _resp(*names):
        return {
            "aggregations": {
                "metrics": {"buckets": [{"key": n, "doc_count": 1} for n in names]}
            }
        }

    async def test_returns_distinct_metric_names_sorted(self, settings):
        client = ESClient(settings)
        client._client = AsyncMock()
        client._client.search.return_value = self._resp("D:", "C:", "E:")
        result = await client.get_metric_names("cvd_all-*", "disk")
        assert result == ["C:", "D:", "E:"]

    async def test_query_filters_category_and_proc(self, settings):
        client = ESClient(settings)
        client._client = AsyncMock()
        client._client.search.return_value = self._resp("total_used_pct")
        await client.get_metric_names("cvd_all-*", "cpu", proc="@system")
        body = client._client.search.call_args.kwargs["body"]
        filters = body["query"]["bool"]["filter"]
        assert {"term": {"EARS_CATEGORY.keyword": "cpu"}} in filters
        assert {"term": {"EARS_PROCNAME.keyword": "@system"}} in filters
        assert body["aggs"]["metrics"]["terms"]["field"] == "EARS_METRIC.keyword"

    async def test_wildcard_proc_omits_proc_filter(self, settings):
        client = ESClient(settings)
        client._client = AsyncMock()
        client._client.search.return_value = self._resp("required")
        await client.get_metric_names("cvd_all-*", "process_watch", proc="*")
        filters = client._client.search.call_args.kwargs["body"]["query"]["bool"]["filter"]
        assert all("EARS_PROCNAME.keyword" not in f.get("term", {}) for f in filters)

    async def test_caches_by_index_category_proc(self, settings):
        client = ESClient(settings)
        client._client = AsyncMock()
        client._client.search.return_value = self._resp("a", "b")
        r1 = await client.get_metric_names("cvd_all-*", "cpu")
        r2 = await client.get_metric_names("cvd_all-*", "cpu")
        assert r1 == r2
        assert client._client.search.call_count == 1
        # different category -> separate cache entry -> new call
        await client.get_metric_names("cvd_all-*", "disk")
        assert client._client.search.call_count == 2

    async def test_returns_empty_on_not_found(self, settings):
        client = ESClient(settings)
        client._client = AsyncMock()
        client._client.search.side_effect = NotFoundError("no such index", {}, {})
        assert await client.get_metric_names("missing_all-*", "cpu") == []

    async def test_returns_empty_on_generic_exception(self, settings):
        client = ESClient(settings)
        client._client = AsyncMock()
        client._client.search.side_effect = ConnectionError("boom")
        assert await client.get_metric_names("cvd_all-*", "cpu") == []

    async def test_negative_cache_with_short_ttl(self, settings):
        clock = _ManualClock()
        client = ESClient(settings, introspect_timer=clock)
        client._client = AsyncMock()
        client._client.search.side_effect = NotFoundError("no such index", {}, {})
        assert await client.get_metric_names("cvd_all-*", "cpu") == []
        clock.advance(240)
        await client.get_metric_names("cvd_all-*", "cpu")
        assert client._client.search.call_count == 1  # still negative-cached
        clock.advance(120)  # past 5-min negative TTL
        client._client.search.side_effect = None
        client._client.search.return_value = self._resp("x")
        assert await client.get_metric_names("cvd_all-*", "cpu") == ["x"]
        assert client._client.search.call_count == 2
