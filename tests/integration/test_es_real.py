"""Elasticsearch 7.11.x integration — real index + search + introspect.

ES 7.x vs 8.x API drift is the highest-risk area in Phase 0 because the
unit tests mocked AsyncElasticsearch entirely. Here we verify against the
real OrbStack ES 7.11.2 container that:
  - `timeout=` and `http_auth=` kwargs are accepted
  - raw dict response works
  - NotFoundError bubbles up as expected
  - introspect_field_type can both discover and cache "unknown"
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import SecretStr

from src.config.settings import AppSettings
from src.es.client import ESClient
from src.es.queries import QueryBuilder

pytestmark = pytest.mark.integration


# ----------------------------------------------------------------------
# Client lifecycle
# ----------------------------------------------------------------------
async def test_es_client_connect_ping_close():
    settings = AppSettings(
        es_hosts=["http://localhost:9200"],
        es_username="",
        es_password=SecretStr(""),
        es_use_ssl=False,
        es_request_timeout=10,
    )
    client = ESClient(settings)
    await client.connect()
    try:
        assert await client.ping() is True
    finally:
        await client.close()
    # After close() ping returns False
    assert await client.ping() is False


# ----------------------------------------------------------------------
# Real index → search round-trip (7.x body= API + raw dict response)
# ----------------------------------------------------------------------
async def test_create_index_and_search_roundtrip(real_es, ns):
    """회귀 가드: ES 7.x API 경로로 실제 search가 raw dict 응답을 반환해야 함."""
    index = f"{ns.es_index_prefix}roundtrip"
    mapping = {
        "mappings": {
            "properties": {
                "@timestamp": {"type": "date"},
                "category": {"type": "keyword"},
                "value": {"type": "double"},
            }
        }
    }
    await real_es.indices.create(index=index, body=mapping)
    try:
        # index 2개 문서
        await real_es.index(
            index=index,
            body={"@timestamp": "2026-04-08T00:00:00Z", "category": "cpu", "value": 42.0},
            refresh="true",
        )
        await real_es.index(
            index=index,
            body={"@timestamp": "2026-04-08T00:01:00Z", "category": "mem", "value": 80.0},
            refresh="true",
        )

        # search with body= parameter (7.x style)
        resp = await real_es.search(
            index=index,
            body={
                "query": {"term": {"category": "cpu"}},
                "size": 10,
            },
        )
        # Raw dict response (7.x), not ObjectApiResponse
        assert isinstance(resp, dict)
        hits = resp["hits"]["hits"]
        assert len(hits) == 1
        assert hits[0]["_source"]["value"] == 42.0
    finally:
        await real_es.indices.delete(index=index, ignore=[404])


# ----------------------------------------------------------------------
# introspect_field_type — real index
# ----------------------------------------------------------------------
async def test_introspect_field_type_returns_mapped_type(ns):
    """실제 index에 매핑된 필드 타입을 정확히 반환해야 함."""
    settings = AppSettings(
        es_hosts=["http://localhost:9200"],
        es_password=SecretStr(""),
    )
    client = ESClient(settings)
    await client.connect()
    index = f"{ns.es_index_prefix}introspect_ok"
    try:
        await client.client.indices.create(
            index=index,
            body={
                "mappings": {
                    "properties": {
                        "category": {"type": "keyword"},
                        "proc": {"type": "text"},
                        "value": {"type": "double"},
                    }
                }
            },
        )
        assert await client.introspect_field_type(index, "category") == "keyword"
        assert await client.introspect_field_type(index, "proc") == "text"
        assert await client.introspect_field_type(index, "value") == "double"
    finally:
        try:
            await client.client.indices.delete(index=index, ignore=[404])
        finally:
            await client.close()


async def test_introspect_field_type_missing_index_returns_unknown(ns):
    """존재하지 않는 인덱스 → 'unknown' 반환 + 캐싱 (재시도 안 함)."""
    settings = AppSettings(
        es_hosts=["http://localhost:9200"],
        es_password=SecretStr(""),
    )
    client = ESClient(settings)
    await client.connect()
    try:
        phantom = f"{ns.es_index_prefix}does_not_exist_ever"
        # 첫 호출 — NotFoundError 경로 트리거
        result1 = await client.introspect_field_type(phantom, "cpu")
        assert result1 == "unknown"
        # 두 번째 호출 — 캐시 히트 (log 나오지 않음)
        result2 = await client.introspect_field_type(phantom, "cpu")
        assert result2 == "unknown"
    finally:
        await client.close()


# ----------------------------------------------------------------------
# QueryBuilder — resolve_index_range (real timezone handling)
# ----------------------------------------------------------------------
def test_resolve_index_range_single_day():
    """윈도우가 자정을 안 넘으면 하나의 인덱스만 반환."""
    settings = AppSettings(local_tz="Asia/Seoul")
    qb = QueryBuilder(settings)
    # 5분 윈도우는 자정 경계 거의 확실히 안 걸침 (매우 드문 실패 제외)
    result = qb.resolve_index_range("ETCH", time_range_minutes=5)
    assert "," not in result
    assert result.startswith("etch_all-")


def test_resolve_index_range_lowercases_process():
    settings = AppSettings(local_tz="Asia/Seoul")
    qb = QueryBuilder(settings)
    result = qb.resolve_index_range("CVD_UPPER", time_range_minutes=1)
    # process는 lowercase로 변환됨
    assert result.startswith("cvd_upper_all-")


def test_build_time_range_filter_shape():
    settings = AppSettings()
    qb = QueryBuilder(settings)
    now = datetime(2026, 4, 8, 12, 0, 0, tzinfo=timezone.utc)
    f = qb.build_time_range_filter(now, window_minutes=10)
    assert f["range"]["@timestamp"]["lte"].startswith("2026-04-08T12:00:00")
    assert f["range"]["@timestamp"]["gte"].startswith("2026-04-08T11:50:00")
    assert f["range"]["@timestamp"]["format"] == "strict_date_optional_time"
