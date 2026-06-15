"""Application settings — loaded from MONITOR_* environment variables.

Version constraints:
- Elasticsearch 7.11.9 (Kibana 7.11.9 pair)
- Redis 5.0.6 (ACL unavailable, simple AUTH only, RESP3 unsupported)
- Zookeeper 3.5.5 (session_timeout 4-40s range, 4lw whitelist defaults to blocked)
"""
from __future__ import annotations

import json
from functools import lru_cache
from typing import Annotated

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class AppSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="MONITOR_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Elasticsearch 7.11.9
    # NoDecode: disable pydantic-settings JSON auto-decode so our
    # field_validator receives the raw string (accepts both "a,b,c" and "[...]").
    es_hosts: Annotated[list[str], NoDecode] = ["http://es-cluster:9200"]
    es_username: str = ""
    es_password: SecretStr = SecretStr("")
    es_use_ssl: bool = False
    es_request_timeout: int = 30
    es_max_retries: int = 3
    # EARS_* 문자열 필드는 운영에서 text + `.keyword` 서브필드로 매핑됨 → term/terms
    # 필터와 terms aggregation은 `.keyword`를 타야 한다. 혹시 bare keyword로
    # 매핑된 클러스터면 ""로 설정해 bare 필드명을 쓴다. (EARS_VALUE/EARS_TIMESTAMP는
    # 숫자/날짜라 미적용 — src/es/queries.py 참고)
    es_keyword_suffix: str = ".keyword"

    # MongoDB (EARS DB shared with Akka)
    mongo_uri: SecretStr = SecretStr("mongodb://localhost:27017")
    mongo_db: str = "EARS"

    # Zookeeper 3.5.5
    zk_hosts: str = "zk1:2181,zk2:2181,zk3:2181"
    zk_root_path: str = "/resource-monitor"
    zk_session_timeout: int = 30  # ZK 3.5.5: 4-40s (tickTime 2s × 2..20)
    zk_sasl_mechanism: str = ""   # e.g. "DIGEST-MD5"; empty = unauthenticated
    zk_sasl_username: str = ""
    zk_sasl_password: SecretStr = SecretStr("")
    # v6 P0-1: cap kazoo.start() so a ZK outage cannot hang lifespan
    # forever (was max_tries=-1). Must stay strictly less than the K8s
    # livenessProbe.initialDelaySeconds (60s in deployment.yaml). The
    # 15s safety margin lets the failure log + close_partial run before
    # liveness fires. P1-4 invariant test pins this against the manifest.
    zk_startup_budget_sec: int = 45

    # Redis 5.0.6 (no ACL)
    # DB 5 는 ARS Redis 인스턴스에서 RMS 전용으로 예약됨 (다른 ARS 서비스와 격리).
    # 실제 prod 값은 k8s/configmap.yaml 의 MONITOR_REDIS_URL 이 override 하며,
    # 이 기본값은 env 가 없는 dev 실행 시 fallback 이다.
    redis_url: str = "redis://redis:6379/5"
    redis_password: SecretStr = SecretStr("")
    redis_key_prefix: str = "RESOURCE_ALERT"
    # Redis HA via Sentinel (옵션). redis_sentinels 가 비어 있으면 redis_url(단일
    # 엔드포인트) 모드, 비어 있지 않으면 Sentinel 모드: 각 "host:port"(보통 26379)
    # 목록으로 sentinel 에 물어 현재 master 를 찾아 연결한다(페일오버 자동 추종).
    # DB 는 URL 의 /N 대신 redis_db 로 지정. sentinel 자체에 requirepass 가 있을
    # 때만 redis_sentinel_password 를 설정한다; 비우면 sentinel 엔 AUTH 를 보내지
    # 않는다(데이터 비번으로 폴백하지 않음 — redis-ha sentinel.auth=false 대응).
    redis_sentinels: Annotated[list[str], NoDecode] = []
    redis_sentinel_master: str = "mymaster"
    redis_sentinel_password: SecretStr = SecretStr("")
    redis_db: int = 0  # Sentinel 모드 DB 번호 (RMS 예약=5 → configmap 에서 명시)

    # Email alert HTTP API (Akka HttpWebServer)
    email_api_url: str = "http://httpwebserver:8080/EmailNotify"
    email_api_timeout: int = 10
    # Akka EmailWorker 가 EMAIL_TEMPLATE / EMAIL_CATEGORY 를 조회할 때 쓰는 app 키.
    # PRD §8.1 에 명시된 필수 필드. 기본값 "ARS" 는 EARS 운영 환경에서
    # SendEmailForRTM 핸들러가 하드코딩으로 사용하는 값과 일치.
    email_app_name: str = "ARS"

    # Grafana links (for alert body)
    grafana_base_url: str = "http://grafana:3000"
    grafana_dashboard_uid: str = ""

    # RMS custom email body (Option C). On by default — it is the intended
    # operating mode and the basis for group-send routing (email_group); set
    # MONITOR_RMS_CUSTOM_BODY_ENABLED=false to fall back to Akka legacy
    # templates. See docs/rms-email-group-routing-decision-2026-06-14.md. The
    # size guards are conservative backstops for the unknown Redis/ESB body cap.
    rms_custom_body_enabled: bool = True
    rms_erb_row_limit: int = 50
    rms_body_byte_cap: int = 256000

    # Scheduler / instance
    scheduler_misfire_grace_time: int = 60
    # Periodic cadence reconcile: each pod re-derives its owned processes'
    # scheduling intervals from Mongo every N seconds and applies only the
    # delta (adds/removes (process,interval) jobs) — so a profile edit that
    # changes the evaluation cadence takes effect without a pod restart or a
    # partition reassignment. 0 disables the loop (write-API/admin triggers
    # still work). See AnalysisScheduler.reconcile.
    scheduler_reconcile_interval_sec: float = 60.0
    instance_id: str = ""
    local_tz: str = "Asia/Seoul"

    # Logging
    log_level: str = "INFO"
    log_format: str = "json"

    # ─── Debug Read-Only ──────────────────────────────────────────────
    # True 면 RMS 가 production 인프라에 대해 "관찰자" 로만 동작한다:
    #   - init_repos: create_collection + create_index 스킵 (schema 변경 없음)
    #     → 컬렉션은 scripts/create-profile-collection.ps1 로 수동 생성
    #   - init_distributed / leader_election 스킵 (ZK 참여 없음)
    #   - scheduler 는 정상 기동 (분석 흐름 관찰 가능)
    #   - cooldown set/clear 는 local TTLCache 만 사용 (Redis 쓰기 없음)
    #   - email_client.send_alert 는 로그만 남기고 즉시 True 반환
    # 절대 production K8s manifests 에 넣지 말 것.
    debug_read_only: bool = False
    # debug 모드에서 scheduler 가 분석할 process 리스트. 비어있으면
    # EqpInfoRepository.get_distinct_processes() 결과 전체를 사용.
    debug_processes: Annotated[list[str], NoDecode] = []

    @field_validator("es_hosts", "debug_processes", "redis_sentinels", mode="before")
    @classmethod
    def parse_string_list(cls, v):
        """Accept both JSON array and comma-separated string.

        ConfigMap writers often prefer comma-separated; env file writers
        sometimes use JSON. Both must yield `list[str]`.
        """
        if isinstance(v, str):
            stripped = v.strip()
            if not stripped:
                return []
            if stripped.startswith("["):
                return json.loads(stripped)
            return [h.strip() for h in stripped.split(",") if h.strip()]
        return v


@lru_cache
def get_settings() -> AppSettings:
    return AppSettings()
