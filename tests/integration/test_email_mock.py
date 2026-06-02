"""Email client integration — real HTTP round-trip to an in-process mock.

이 테스트는 실제 Akka 서버 없이 `EmailAlertClient`가 httpx 실 요청을 보내고
응답을 해석하는 전체 경로를 검증한다. unit test는 AsyncMock으로 httpx를
mock했기 때문에 serialization 레이어의 버그(예: Pydantic alias 변환 누락)를
잡지 못한다.
"""
from __future__ import annotations

import pytest

from src.alert.email_client import EmailAlertClient
from src.alert.models import EmailAlertRequest
from src.config.settings import AppSettings

pytestmark = pytest.mark.integration


def _make_request() -> EmailAlertRequest:
    return EmailAlertRequest(
        hostname="test-host",
        ip="10.0.0.1",
        app="ARS",
        process="TEST_PROC",
        eqp_model="TEST_MODEL_ABC",
        line="L1",
        code="RESOURCE_MONITOR",
        subcode="WARNING",
        variables={"THRESHOLD": "90", "VALUE": "95"},
    )


async def _make_client(url: str) -> EmailAlertClient:
    settings = AppSettings(email_api_url=url, email_api_timeout=5)
    client = EmailAlertClient(settings)
    await client.connect()
    return client


# ----------------------------------------------------------------------
# 1. success path — actual Akka response is capital-S "Success"
# ----------------------------------------------------------------------
async def test_send_alert_success_capital(mock_email_server):
    """Akka's EmailWorker returns ``HttpResponse("Success", "")``.
    Verifies the case-insensitive comparison treats this as success and
    that the payload includes every field Akka requires."""
    mock_email_server["next_response"] = {"result": "Success", "message": ""}
    mock_email_server["next_status"] = 200
    mock_email_server["received"].clear()

    client = await _make_client(mock_email_server["url"])
    try:
        result = await client.send_alert(_make_request())
    finally:
        await client.close()

    assert result is True
    assert len(mock_email_server["received"]) == 1
    received = mock_email_server["received"][0]
    # Payload must use "model" (Akka key), not "eqp_model"
    assert received["model"] == "TEST_MODEL_ABC"
    assert "eqp_model" not in received
    # `app` is required by Akka's EmailHttpDataFormat
    assert received["app"] == "ARS"
    assert received["code"] == "RESOURCE_MONITOR"
    assert received["variables"] == {"THRESHOLD": "90", "VALUE": "95"}
    # Sanity check: every key Akka expects is present
    expected_keys = {
        "hostname", "ip", "app", "process", "model", "line",
        "code", "subcode", "variables",
    }
    assert set(received.keys()) == expected_keys


async def test_send_alert_success_lowercase_also_accepted(mock_email_server):
    """Defensive: a hypothetical lowercase ``"success"`` from Akka is
    also accepted (case-insensitive comparison)."""
    mock_email_server["next_response"] = {"result": "success", "message": "send ok"}
    mock_email_server["next_status"] = 200
    mock_email_server["received"].clear()

    client = await _make_client(mock_email_server["url"])
    try:
        result = await client.send_alert(_make_request())
    finally:
        await client.close()

    assert result is True


# ----------------------------------------------------------------------
# 2. application-level failure
# ----------------------------------------------------------------------
async def test_send_alert_app_failure(mock_email_server):
    """Akka returns 200 but result=Fail — client must return False."""
    mock_email_server["next_response"] = {"result": "Fail", "message": "SMTP refused"}
    mock_email_server["next_status"] = 200
    mock_email_server["received"].clear()

    client = await _make_client(mock_email_server["url"])
    try:
        result = await client.send_alert(_make_request())
    finally:
        await client.close()

    assert result is False
    assert len(mock_email_server["received"]) == 1


# ----------------------------------------------------------------------
# 3. HTTP 5xx
# ----------------------------------------------------------------------
async def test_send_alert_http_error(mock_email_server):
    mock_email_server["next_response"] = {"error": "internal"}
    mock_email_server["next_status"] = 500
    mock_email_server["received"].clear()

    client = await _make_client(mock_email_server["url"])
    try:
        result = await client.send_alert(_make_request())
    finally:
        await client.close()

    assert result is False
    assert len(mock_email_server["received"]) == 1


# ----------------------------------------------------------------------
# 4. timeout — server delays past client timeout
# ----------------------------------------------------------------------
async def test_send_alert_timeout(mock_email_server):
    mock_email_server["next_response"] = {"result": "Success", "message": ""}
    mock_email_server["next_status"] = 200
    mock_email_server["delay_sec"] = 2.0
    mock_email_server["received"].clear()

    # Client timeout shorter than server delay
    settings = AppSettings(
        email_api_url=mock_email_server["url"], email_api_timeout=1
    )
    client = EmailAlertClient(settings)
    await client.connect()
    try:
        result = await client.send_alert(_make_request())
    finally:
        await client.close()
        mock_email_server["delay_sec"] = 0.0  # reset for siblings

    assert result is False
