"""Tests for tools.email_mock — mock /EmailNotify 서버의 순수 로직.

(주의: 기존 tests/integration/test_email_mock.py 와는 다른 파일.
 이쪽은 운영 EmailAlertClient 가 아니라, 우리가 만든 mock 서버의
 수신 포맷팅/응답 본문 순수 함수를 테스트한다.)
"""
from __future__ import annotations

import pytest

from tools.email_mock import format_received, success_response_body

pytestmark = pytest.mark.unit


def _sample_payload() -> dict:
    return {
        "hostname": "DEV-LOCALPC-01",
        "ip": "10.0.0.42",
        "app": "ARS",
        "process": "DEV_PROC",
        "model": "MODEL-DEV-ABC",
        "line": "L-DEV",
        "code": "RESOURCE_MONITOR",
        "subcode": "CPU_WARNING",
        "variables": {
            "Severity": "WARNING",
            "Category": "CPU",
            "MetricName": "total_used_pct",
            "설명": "씨피유 사용률 초과",
        },
    }


# ----------------------------------------------------------------------
# Cycle B1 — 성공 응답 본문 (Akka 는 대문자 "Success")
# ----------------------------------------------------------------------
def test_success_response_is_capital_success():
    assert success_response_body() == {"result": "Success", "message": ""}


# ----------------------------------------------------------------------
# Cycle B2 — 수신 payload 포맷팅: 헤더 핵심 필드 포함
# ----------------------------------------------------------------------
def test_format_received_includes_header_fields():
    out = format_received(_sample_payload(), seq=1, ts="2026-06-02T10:00:00")

    for value in (
        "DEV-LOCALPC-01",  # hostname
        "10.0.0.42",       # ip
        "ARS",             # app
        "DEV_PROC",        # process
        "MODEL-DEV-ABC",   # model
        "L-DEV",           # line
        "RESOURCE_MONITOR",  # code
        "CPU_WARNING",     # subcode
    ):
        assert value in out


# ----------------------------------------------------------------------
# Cycle B3 — variables 출력 + 한글 보존 + 순번 표기
# ----------------------------------------------------------------------
def test_format_received_renders_variables_and_unicode():
    out = format_received(_sample_payload(), seq=1, ts="2026-06-02T10:00:00")

    # variables 키/값
    assert "MetricName" in out
    assert "total_used_pct" in out
    # 한글이 \uXXXX 로 깨지지 않고 그대로 출력 (ensure_ascii=False)
    assert "씨피유 사용률 초과" in out
    # 순번 표기
    assert "#1" in out


# ----------------------------------------------------------------------
# Cycle B4 — 잘못된(키 누락) payload 방어
# ----------------------------------------------------------------------
def test_format_received_handles_missing_keys():
    out = format_received({"app": "ARS"}, seq=2, ts="2026-06-02T10:00:01")

    assert "ARS" in out
    assert "<none>" in out  # 누락 필드는 <none> 으로 표기
    assert "#2" in out
