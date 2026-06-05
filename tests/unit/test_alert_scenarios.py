"""Tests for tools.alert_scenarios — local mock-email 시나리오 빌더.

이 모듈은 운영 코드(`src/analyzer/alert_builder.py`)의 `build_alert_request` 와
`ThresholdBreach` 를 재사용해, 개발 PC 에서 손으로 발송해 볼 샘플
`EmailAlertRequest` 를 만든다. 따라서 생성되는 payload 는 실제 분석 엔진이
내보내는 것과 동일한 형태여야 한다.
"""
from __future__ import annotations

import pytest

from src.alert.models import EmailAlertRequest
from src.config.constants import (
    ALERT_CATEGORY_CPU,
    ALERT_CATEGORY_DISK,
    ALERT_CATEGORY_PROCESS_WATCH,
    ALERT_CODE_RESOURCE_MONITOR,
)
from tools.alert_scenarios import build_alert

pytestmark = pytest.mark.unit


# ----------------------------------------------------------------------
# Cycle A1 — CPU WARNING 시나리오
# ----------------------------------------------------------------------
def test_build_cpu_scenario_sets_threshold_breach():
    req = build_alert("cpu", app_name="ARS")

    assert isinstance(req, EmailAlertRequest)
    assert req.code == ALERT_CODE_RESOURCE_MONITOR
    # subcode 는 "{category}_{severity}" 형식 (alert_builder.py 규칙)
    assert req.subcode.startswith(ALERT_CATEGORY_CPU + "_")
    # 임계 초과를 표현 — current >= threshold
    current = float(req.variables["CurrentValue"])
    threshold = float(req.variables["Threshold"])
    assert current >= threshold


# ----------------------------------------------------------------------
# Cycle A2 — app 필드가 인자에서 온다 (config 반영 증명 지점)
# ----------------------------------------------------------------------
def test_build_alert_app_comes_from_arg():
    assert build_alert("cpu", app_name="EARS").app == "EARS"
    assert build_alert("cpu", app_name="ARS").app == "ARS"


# ----------------------------------------------------------------------
# Cycle A3 — DISK CRITICAL 시나리오
# ----------------------------------------------------------------------
def test_build_disk_scenario_is_critical():
    req = build_alert("disk", app_name="ARS")

    assert req.subcode.startswith(ALERT_CATEGORY_DISK + "_")
    assert req.variables["Severity"] == "CRITICAL"
    assert req.subcode.endswith("CRITICAL")
    current = float(req.variables["CurrentValue"])
    threshold = float(req.variables["Threshold"])
    assert current >= threshold


# ----------------------------------------------------------------------
# Cycle A4 — PROCESS down (state_check) 시나리오
# ----------------------------------------------------------------------
def test_build_process_scenario_signals_down():
    req = build_alert("process", app_name="ARS")

    assert req.subcode.startswith(ALERT_CATEGORY_PROCESS_WATCH + "_")
    # 필수 프로세스 미검출 → fact 가 "required" 계열 measure 를 가리킴
    assert "required" in req.variables["MetricName"]


# ----------------------------------------------------------------------
# Cycle A5 — variables 필수 치환 키 완비
# ----------------------------------------------------------------------
@pytest.mark.parametrize("scenario", ["cpu", "disk", "process"])
def test_scenario_variables_have_required_keys(scenario):
    req = build_alert(scenario, app_name="ARS")

    required_keys = {
        "Severity",
        "Category",
        "MetricName",
        "CurrentValue",
        "Threshold",
        "WindowMin",
        "GrafanaUrl",
    }
    assert required_keys <= set(req.variables.keys())


# ----------------------------------------------------------------------
# Cycle A6 — 잘못된 시나리오 거부
# ----------------------------------------------------------------------
def test_build_alert_rejects_unknown_scenario():
    with pytest.raises(ValueError):
        build_alert("nope", app_name="ARS")


# ----------------------------------------------------------------------
# Cycle A7 — to_payload() 가 Akka 스키마(model 키)와 호환
# ----------------------------------------------------------------------
def test_scenario_payload_uses_model_key_not_eqp_model():
    payload = build_alert("cpu", app_name="ARS").to_payload()

    assert "model" in payload
    assert "eqp_model" not in payload
    assert payload["app"] == "ARS"
