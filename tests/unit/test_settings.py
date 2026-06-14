"""Tests for src.config.settings (AppSettings)."""
import json

import pytest
from pydantic import SecretStr

from src.config.settings import AppSettings, get_settings


@pytest.fixture(autouse=True)
def _isolate_from_dotenv(monkeypatch, tmp_path):
    """이 모듈의 테스트는 빌트인 기본값을 검증한다.

    개발자가 프로젝트 루트에 ``.env`` 를 두면 (로컬 실행/디버깅 시 흔함)
    pydantic-settings 가 cwd 상대경로로 그 파일을 읽어 기본값 테스트가
    오염된다. cwd 를 빈 임시 디렉토리로 옮겨 ``.env`` 를 못 찾게 격리한다.
    (env-override 테스트는 monkeypatch.setenv 를 쓰므로 영향 없음.)
    """
    monkeypatch.chdir(tmp_path)


@pytest.mark.unit
class TestAppSettingsDefaults:
    def test_default_es_hosts_is_list(self):
        s = AppSettings()
        assert isinstance(s.es_hosts, list)
        assert len(s.es_hosts) >= 1

    def test_default_mongo_db_is_ears(self):
        s = AppSettings()
        assert s.mongo_db == "EARS"

    def test_rms_custom_body_defaults(self):
        """Option C is the intended operating mode → on by default when env
        unset (see docs/rms-email-group-routing-decision-2026-06-14.md); set
        MONITOR_RMS_CUSTOM_BODY_ENABLED=false to opt out. Conservative size
        guards unchanged."""
        s = AppSettings()
        assert s.rms_custom_body_enabled is True
        assert s.rms_erb_row_limit == 50
        assert s.rms_body_byte_cap == 256000

    def test_default_zk_session_timeout_in_range(self):
        """ZK 3.5.5 constraint: 4 <= timeout <= 40 (tickTime 2s × 2~20)."""
        s = AppSettings()
        assert 4 <= s.zk_session_timeout <= 40

    def test_default_zk_startup_budget_under_liveness_initial_delay(self):
        """v6 P0-1: zk_startup_budget_sec must be strictly less than the
        K8s livenessProbe.initialDelaySeconds (60s) so the pod fails-fast
        before liveness ever fires. Default 45 leaves a 15s safety margin.
        """
        s = AppSettings()
        assert s.zk_startup_budget_sec == 45
        # Hard invariant: must leave at least 10s margin under liveness
        # initialDelaySeconds=60. P1-4 will pin this against deployment.yaml.
        assert s.zk_startup_budget_sec <= 50

    def test_default_redis_key_prefix(self):
        s = AppSettings()
        assert s.redis_key_prefix == "RESOURCE_ALERT"

    def test_default_log_format_json(self):
        s = AppSettings()
        assert s.log_format == "json"

    def test_default_local_tz_asia_seoul(self):
        s = AppSettings()
        assert s.local_tz == "Asia/Seoul"


@pytest.mark.unit
class TestSecretStrFields:
    def test_es_password_is_secret_str(self):
        s = AppSettings()
        assert isinstance(s.es_password, SecretStr)

    def test_mongo_uri_is_secret_str(self):
        s = AppSettings()
        assert isinstance(s.mongo_uri, SecretStr)

    def test_redis_password_is_secret_str(self):
        s = AppSettings()
        assert isinstance(s.redis_password, SecretStr)

    def test_zk_sasl_password_is_secret_str(self):
        s = AppSettings()
        assert isinstance(s.zk_sasl_password, SecretStr)

    def test_secret_not_in_repr(self):
        """SecretStr should mask the value in str/repr."""
        s = AppSettings(mongo_uri="mongodb://user:supersecret@host:27017")
        assert "supersecret" not in repr(s)
        assert "supersecret" not in str(s.mongo_uri)


@pytest.mark.unit
class TestEnvPrefix:
    def test_env_prefix_monitor_applies(self, monkeypatch):
        monkeypatch.setenv("MONITOR_MONGO_DB", "TEST_DB")
        s = AppSettings()
        assert s.mongo_db == "TEST_DB"

    def test_env_prefix_for_log_level(self, monkeypatch):
        monkeypatch.setenv("MONITOR_LOG_LEVEL", "DEBUG")
        s = AppSettings()
        assert s.log_level == "DEBUG"


@pytest.mark.unit
class TestEsHostsParsing:
    def test_comma_separated_string_to_list(self, monkeypatch):
        monkeypatch.setenv("MONITOR_ES_HOSTS", "http://a:9200,http://b:9200,http://c:9200")
        s = AppSettings()
        assert s.es_hosts == ["http://a:9200", "http://b:9200", "http://c:9200"]

    def test_json_array_string_to_list(self, monkeypatch):
        monkeypatch.setenv(
            "MONITOR_ES_HOSTS", json.dumps(["http://x:9200", "http://y:9200"])
        )
        s = AppSettings()
        assert s.es_hosts == ["http://x:9200", "http://y:9200"]

    def test_single_host_no_comma(self, monkeypatch):
        monkeypatch.setenv("MONITOR_ES_HOSTS", "http://only:9200")
        s = AppSettings()
        assert s.es_hosts == ["http://only:9200"]

    def test_trim_whitespace_in_comma_list(self, monkeypatch):
        monkeypatch.setenv("MONITOR_ES_HOSTS", " http://a:9200 , http://b:9200 ")
        s = AppSettings()
        assert s.es_hosts == ["http://a:9200", "http://b:9200"]

    def test_empty_items_are_filtered(self, monkeypatch):
        monkeypatch.setenv("MONITOR_ES_HOSTS", "http://a:9200,,http://b:9200,")
        s = AppSettings()
        assert s.es_hosts == ["http://a:9200", "http://b:9200"]


@pytest.mark.unit
class TestDebugReadOnly:
    """Debug 모드 설정. prod 데이터에 대한 read-only 디버깅 (쓰기 경로 전부 차단)."""

    def test_debug_read_only_defaults_false(self):
        """운영 기본값은 반드시 False — prod 에서 실수로 활성화되면 안 됨."""
        s = AppSettings()
        assert s.debug_read_only is False

    def test_debug_processes_defaults_empty(self):
        s = AppSettings()
        assert s.debug_processes == []

    def test_debug_read_only_env_override(self, monkeypatch):
        monkeypatch.setenv("MONITOR_DEBUG_READ_ONLY", "true")
        s = AppSettings()
        assert s.debug_read_only is True

    def test_debug_processes_comma_separated(self, monkeypatch):
        monkeypatch.setenv("MONITOR_DEBUG_PROCESSES", "ETCH,CVD,LITHO")
        s = AppSettings()
        assert s.debug_processes == ["ETCH", "CVD", "LITHO"]

    def test_debug_processes_json_array(self, monkeypatch):
        monkeypatch.setenv("MONITOR_DEBUG_PROCESSES", json.dumps(["A", "B"]))
        s = AppSettings()
        assert s.debug_processes == ["A", "B"]

    def test_debug_processes_trims_whitespace(self, monkeypatch):
        monkeypatch.setenv("MONITOR_DEBUG_PROCESSES", " ETCH , CVD ")
        s = AppSettings()
        assert s.debug_processes == ["ETCH", "CVD"]


@pytest.mark.unit
class TestGetSettingsCache:
    def test_get_settings_is_cached(self):
        s1 = get_settings()
        s2 = get_settings()
        assert s1 is s2

    def test_cache_clear_produces_new_instance(self, monkeypatch):
        s1 = get_settings()
        get_settings.cache_clear()
        monkeypatch.setenv("MONITOR_LOG_LEVEL", "ERROR")
        s2 = get_settings()
        assert s2.log_level == "ERROR"
        assert s1 is not s2
