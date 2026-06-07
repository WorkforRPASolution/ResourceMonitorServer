"""Elasticsearch 7.11.9 async client wrapper.

ES 7.x vs 8.x differences this wrapper normalizes:
- Auth kwarg is `http_auth=(user, pass)` (8.x uses `basic_auth`)
- Timeout kwarg is `timeout=seconds` (8.x uses `request_timeout`)
- `search()` takes `body={...}` (8.x prefers named kwargs)
- Response is a raw `dict` (8.x wraps in `ObjectApiResponse`)

`introspect_field_type` is lazy and never raises on missing indexes — the
service must start even if today's index does not exist yet (very common at
midnight or after a long weekend).

v6 P1-5 — introspect cache TTL:
    Earlier the negative cache was a permanent ``set``, so an index that did
    not exist when the pod booted stayed "unknown" forever — even after the
    nightly index roll created it. Both caches are now ``TTLCache``:
    positive results live 10 min (mappings rarely change), negative results
    only 5 min (so we *do* retry once per 5 min and pick up newly created
    indexes without a pod restart).
"""
from __future__ import annotations

import structlog
from cachetools import TTLCache
from elasticsearch import AsyncElasticsearch
from elasticsearch.exceptions import NotFoundError

from src.config.settings import AppSettings
from src.es.queries import CATEGORY_FIELD, METRIC_FIELD, PROC_FIELD, keyword_field

logger = structlog.get_logger(__name__)

# v6 P1-5 — positive results are stable, keep them long. Negative results
# (NotFound, transport error, missing field) get a shorter TTL so the next
# query retries instead of being stuck on stale "unknown" forever.
_INTROSPECT_POSITIVE_TTL = 600  # 10 min
_INTROSPECT_NEGATIVE_TTL = 300  # 5 min
_INTROSPECT_CACHE_MAXSIZE = 500

_METRIC_NAMES_TERMS_SIZE = 1000  # distinct EARS_METRIC values per category

# Sentinel for negative caching of get_metric_names — distinguishes
# "we queried and got nothing" from "we never queried".
_METRIC_NAMES_EMPTY: list[str] = []


class ESClient:
    """Async ES 7.x client.

    Lifecycle: `connect()` → used → `close()`. `ping()` is safe on an
    unconnected instance (returns False).
    """

    def __init__(
        self,
        settings: AppSettings,
        *,
        introspect_positive_ttl: int = _INTROSPECT_POSITIVE_TTL,
        introspect_negative_ttl: int = _INTROSPECT_NEGATIVE_TTL,
        introspect_timer=None,
    ) -> None:
        """Build the client. The TTL/timer kwargs exist purely for tests —
        production code never passes them. cachetools imports ``monotonic``
        directly via ``from time import monotonic``, so ``time-machine``
        cannot patch it; tests use ``introspect_timer`` to drive the TTL
        clock manually instead of waiting in real time.
        """
        self._settings = settings
        self._client: AsyncElasticsearch | None = None
        # v6 P1-5: bounded TTL caches. Positive holds successful field types
        # for 10 min; negative holds "unknown" markers for 5 min so we retry
        # after newly-rolled indexes appear without a pod restart.
        cache_kwargs: dict = {"maxsize": _INTROSPECT_CACHE_MAXSIZE}
        if introspect_timer is not None:
            cache_kwargs["timer"] = introspect_timer
        self._field_types: TTLCache[str, str] = TTLCache(
            ttl=introspect_positive_ttl, **cache_kwargs
        )
        self._introspect_negative: TTLCache[str, bool] = TTLCache(
            ttl=introspect_negative_ttl, **cache_kwargs
        )
        # v2: cache for get_metric_names (keyed by index+category+proc),
        # same dual-TTL strategy as the per-field introspect caches.
        self._metric_names_positive: TTLCache[str, list[str]] = TTLCache(
            ttl=introspect_positive_ttl, **cache_kwargs
        )
        self._metric_names_negative: TTLCache[str, bool] = TTLCache(
            ttl=introspect_negative_ttl, **cache_kwargs
        )

    @property
    def client(self) -> AsyncElasticsearch:
        if self._client is None:
            raise RuntimeError("ESClient.connect() must be called first")
        return self._client

    async def connect(self) -> None:
        """Build the AsyncElasticsearch client and verify reachability.

        v6 P0-3: a startup ping was added so a typo in MONITOR_ES_HOSTS or
        a wrong port fails the boot loudly instead of silently passing and
        only blowing up at first query. The existing ``ping()`` method is
        reused — it never raises, so a False return becomes a RuntimeError
        that flows through ``init_infra``'s ``close_partial`` rollback.
        """
        kwargs: dict = {
            "hosts": self._settings.es_hosts,
            "timeout": self._settings.es_request_timeout,  # 7.x: timeout
            "max_retries": self._settings.es_max_retries,
            "retry_on_timeout": True,
        }
        if self._settings.es_username:
            kwargs["http_auth"] = (  # 7.x: http_auth, 8.x: basic_auth
                self._settings.es_username,
                self._settings.es_password.get_secret_value(),
            )
        if self._settings.es_use_ssl:
            kwargs["use_ssl"] = True
            kwargs["verify_certs"] = True
        self._client = AsyncElasticsearch(**kwargs)
        if not await self.ping():
            raise RuntimeError("es_startup_ping_failed")

    async def ping(self) -> bool:
        """True if the cluster responds. Never raises."""
        if self._client is None:
            return False
        try:
            return bool(await self._client.ping())
        except Exception as e:
            logger.warning("es_ping_failed", error=str(e))
            return False

    async def close(self) -> None:
        if self._client is not None:
            try:
                await self._client.close()
            except Exception as e:
                logger.warning("es_close_failed", error=str(e))
            finally:
                self._client = None

    async def introspect_field_type(self, index_pattern: str, field: str) -> str:
        """Return the mapping type of `field` in `index_pattern`, or ``"unknown"``.

        Lazy + TTL-cached. Successful results live 10 min; failures live 5
        min and are then retried, so newly-rolled indexes get picked up
        without a pod restart (v6 P1-5).
        """
        cache_key = f"{index_pattern}:{field}"
        if cache_key in self._field_types:
            return self._field_types[cache_key]
        if cache_key in self._introspect_negative:
            return "unknown"

        try:
            mapping = await self.client.indices.get_mapping(
                index=index_pattern, allow_no_indices=True
            )
            # ES 7.x response shape: {index_name: {"mappings": {"properties": {...}}}}
            for idx_data in mapping.values():
                props = idx_data.get("mappings", {}).get("properties", {})
                if field in props:
                    field_type = props[field].get("type", "text")
                    self._field_types[cache_key] = field_type
                    return field_type
            logger.debug(
                "es_introspect_field_missing", pattern=index_pattern, field=field
            )
        except NotFoundError:
            logger.warning(
                "es_index_not_found_for_introspect", pattern=index_pattern
            )
        except Exception as e:
            logger.warning(
                "es_introspect_failed", pattern=index_pattern, error=str(e)
            )
        # Negative result — short TTL so the next query retries.
        self._introspect_negative[cache_key] = True
        return "unknown"

    async def get_metric_names(
        self, index_pattern: str, category: str, proc: str = "@system"
    ) -> list[str]:
        """Return the distinct ``EARS_METRIC`` values for a category (+proc).

        v2 resolves wildcard metric patterns against the metric *instances* that
        actually exist, discovered by a ``terms`` aggregation on EARS_METRIC
        rather than the index mapping (every metric shares the EARS_VALUE
        column). Cached with the dual-TTL strategy: positive (10 min) for stable
        instance sets, negative (5 min) for missing indices that appear after a
        nightly roll. ``proc == "*"`` discovers across all procnames.
        """
        cache_key = f"{index_pattern}:{category}:{proc}"
        if cache_key in self._metric_names_positive:
            return self._metric_names_positive[cache_key]
        if cache_key in self._metric_names_negative:
            return _METRIC_NAMES_EMPTY

        # EARS_* strings are text+.keyword in prod → filter/aggregate on .keyword
        # (configurable via settings.es_keyword_suffix). See src/es/queries.py.
        cat_field = keyword_field(CATEGORY_FIELD, self._settings)
        proc_field = keyword_field(PROC_FIELD, self._settings)
        metric_field = keyword_field(METRIC_FIELD, self._settings)
        filters: list[dict] = [{"term": {cat_field: category}}]
        if proc != "*":
            filters.append({"term": {proc_field: proc}})
        body = {
            "size": 0,
            "query": {"bool": {"filter": filters}},
            "aggs": {
                "metrics": {
                    "terms": {"field": metric_field, "size": _METRIC_NAMES_TERMS_SIZE}
                }
            },
        }
        try:
            resp = await self.client.search(index=index_pattern, body=body)
            buckets = (
                resp.get("aggregations", {}).get("metrics", {}).get("buckets", [])
            )
            result = sorted(b["key"] for b in buckets)
            self._metric_names_positive[cache_key] = result
            return result
        except NotFoundError:
            logger.warning("es_index_not_found_for_metric_names", pattern=index_pattern)
        except Exception as e:
            logger.warning(
                "es_get_metric_names_failed", pattern=index_pattern, error=str(e)
            )
        self._metric_names_negative[cache_key] = True
        return _METRIC_NAMES_EMPTY
