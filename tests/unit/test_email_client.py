"""Tests for src.alert.email_client.

Akka's EmailWorker actually returns ``{"result":"Success","message":""}``
(capital S, empty message). The client compares case-insensitively so both
``"Success"`` and ``"success"`` are accepted as the success path; anything
else (``"Fail"``, missing field, non-string) is treated as a failure and
appended to the in-memory outbox.
"""
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from src.alert.email_client import EmailAlertClient
from src.alert.models import EmailAlertRequest
from src.config.settings import AppSettings


@pytest.fixture
def settings() -> AppSettings:
    return AppSettings(
        email_api_url="http://httpwebserver:8080/EmailNotify",
        email_api_timeout=5,
    )


@pytest.fixture
def sample_request() -> EmailAlertRequest:
    return EmailAlertRequest(
        hostname="HOST01",
        ip="10.0.0.1",
        app="ARS",
        process="CVD",
        eqp_model="ABC123",
        line="LINE1",
        code="RESOURCE_MONITOR",
        subcode="WARNING",
        variables={"METRIC": "cpu.total_used_pct", "VALUE": "92.3"},
    )


def _mock_response(status_code: int, json_body: dict) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.text = str(json_body)
    resp.json.return_value = json_body
    resp.raise_for_status = MagicMock()
    if status_code >= 400:
        resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            "bad", request=MagicMock(), response=resp
        )
    return resp


@pytest.mark.unit
class TestEmailClientConnect:
    """v6 P0-3: connect() must verify the Akka API is reachable.

    Without this, a typo in MONITOR_EMAIL_API_URL boots cleanly and the
    failure only surfaces when the first alert tries to send — by then
    the operator has no idea the address is wrong.
    """

    async def test_connect_calls_health_check(self, settings):
        client = EmailAlertClient(settings)
        with pytest.MonkeyPatch.context() as mp:
            created: dict = {}

            class FakeAsyncClient:
                def __init__(self, *args, **kwargs):
                    created["instance"] = self

                async def request(self, method, url):
                    created["request"] = (method, url)
                    return MagicMock(status_code=200)

                async def aclose(self):
                    pass

            mp.setattr("src.alert.email_client.httpx.AsyncClient", FakeAsyncClient)
            await client.connect()
        assert created["request"] == ("HEAD", "http://httpwebserver:8080/EmailNotify")

    async def test_connect_raises_when_health_check_fails(self, settings):
        client = EmailAlertClient(settings)
        with pytest.MonkeyPatch.context() as mp:

            class FakeAsyncClient:
                def __init__(self, *args, **kwargs):
                    pass

                async def request(self, method, url):
                    raise httpx.ConnectError("dead")

                async def aclose(self):
                    pass

            mp.setattr("src.alert.email_client.httpx.AsyncClient", FakeAsyncClient)
            with pytest.raises(RuntimeError, match="email_startup_health_check_failed"):
                await client.connect()

    async def test_connect_skips_health_check_in_debug_mode(self):
        """Debug instance must not health-check production email API
        — the operator may be running this on a laptop with no network
        access to the prod Akka box. Boot must succeed regardless."""
        debug_settings = AppSettings(
            email_api_url="http://httpwebserver:8080/EmailNotify",
            email_api_timeout=5,
            debug_read_only=True,
        )
        client = EmailAlertClient(debug_settings)
        with pytest.MonkeyPatch.context() as mp:
            request_called = {"n": 0}

            class FakeAsyncClient:
                def __init__(self, *args, **kwargs):
                    pass

                async def request(self, method, url):
                    request_called["n"] += 1
                    raise httpx.ConnectError("would be dead but never called")

                async def aclose(self):
                    pass

            mp.setattr("src.alert.email_client.httpx.AsyncClient", FakeAsyncClient)
            await client.connect()  # must not raise
        assert request_called["n"] == 0


@pytest.mark.unit
class TestSendAlertSuccess:
    async def test_returns_true_on_capital_success(
        self, settings, sample_request
    ):
        """Akka's actual response: ``{"result":"Success","message":""}``.

        Source: ``EmailWorker.scala`` (both ``SendEmail`` and
        ``SendEmailForRTM``) emit capital-S ``"Success"``. This is the
        primary success-path case the client must recognize.
        """
        client = EmailAlertClient(settings)
        client._http_client = AsyncMock()
        client._http_client.post.return_value = _mock_response(
            200, {"result": "Success", "message": ""}
        )
        assert await client.send_alert(sample_request) is True

    async def test_returns_true_on_lowercase_success(
        self, settings, sample_request
    ):
        """Comparison is case-insensitive, so a hypothetical lowercase
        ``"success"`` from Akka would also be accepted (defensive)."""
        client = EmailAlertClient(settings)
        client._http_client = AsyncMock()
        client._http_client.post.return_value = _mock_response(
            200, {"result": "success", "message": "send ok"}
        )
        assert await client.send_alert(sample_request) is True

    async def test_returns_false_on_lowercase_fail(
        self, settings, sample_request
    ):
        client = EmailAlertClient(settings)
        client._http_client = AsyncMock()
        client._http_client.post.return_value = _mock_response(
            200, {"result": "Fail", "message": "template not found"}
        )
        assert await client.send_alert(sample_request) is False


@pytest.mark.unit
class TestSendAlertErrorCases:
    async def test_returns_false_on_timeout(self, settings, sample_request):
        client = EmailAlertClient(settings)
        client._http_client = AsyncMock()
        client._http_client.post.side_effect = httpx.TimeoutException("slow")
        assert await client.send_alert(sample_request) is False

    async def test_returns_false_on_connect_error(self, settings, sample_request):
        client = EmailAlertClient(settings)
        client._http_client = AsyncMock()
        client._http_client.post.side_effect = httpx.ConnectError("refused")
        assert await client.send_alert(sample_request) is False

    async def test_returns_false_on_5xx(self, settings, sample_request):
        client = EmailAlertClient(settings)
        client._http_client = AsyncMock()
        client._http_client.post.return_value = _mock_response(
            503, {"result": "error"}
        )
        assert await client.send_alert(sample_request) is False

    async def test_returns_false_on_invalid_json(self, settings, sample_request):
        client = EmailAlertClient(settings)
        resp = MagicMock()
        resp.status_code = 200
        resp.text = "not json"
        resp.raise_for_status = MagicMock()
        resp.json.side_effect = ValueError("not json")
        client._http_client = AsyncMock()
        client._http_client.post.return_value = resp
        assert await client.send_alert(sample_request) is False

    async def test_returns_false_on_missing_result_field(
        self, settings, sample_request
    ):
        client = EmailAlertClient(settings)
        client._http_client = AsyncMock()
        client._http_client.post.return_value = _mock_response(
            200, {"message": "missing result field"}
        )
        assert await client.send_alert(sample_request) is False


@pytest.mark.unit
class TestSendAlertRequestShape:
    async def test_posts_to_configured_url(self, settings, sample_request):
        client = EmailAlertClient(settings)
        client._http_client = AsyncMock()
        client._http_client.post.return_value = _mock_response(
            200, {"result": "Success", "message": ""}
        )
        await client.send_alert(sample_request)
        call = client._http_client.post.call_args
        assert call.args[0] == "http://httpwebserver:8080/EmailNotify"

    async def test_posts_json_with_model_field(self, settings, sample_request):
        """Payload must use `model`, not `eqp_model` (Akka's field name)."""
        client = EmailAlertClient(settings)
        client._http_client = AsyncMock()
        client._http_client.post.return_value = _mock_response(
            200, {"result": "Success", "message": ""}
        )
        await client.send_alert(sample_request)
        kwargs = client._http_client.post.call_args.kwargs
        assert kwargs["json"]["model"] == "ABC123"
        assert "eqp_model" not in kwargs["json"]

    async def test_posts_json_with_app_field(self, settings, sample_request):
        """Payload must include `app` — Akka's EmailHttpDataFormat
        requires it and uses it as a key into EMAIL_TEMPLATE."""
        client = EmailAlertClient(settings)
        client._http_client = AsyncMock()
        client._http_client.post.return_value = _mock_response(
            200, {"result": "Success", "message": ""}
        )
        await client.send_alert(sample_request)
        kwargs = client._http_client.post.call_args.kwargs
        assert kwargs["json"]["app"] == "ARS"


@pytest.mark.unit
class TestDebugReadOnlyGuard:
    """★ Debug Read-Only mode: send_alert must never hit the HTTP client.
    A debug instance connected to the production email API could send real
    emails to operators if we forget this guard."""

    async def test_debug_mode_skips_http_post(self, sample_request):
        debug_settings = AppSettings(
            email_api_url="http://httpwebserver:8080/EmailNotify",
            email_api_timeout=5,
            debug_read_only=True,
        )
        client = EmailAlertClient(debug_settings)
        client._http_client = AsyncMock()

        result = await client.send_alert(sample_request)

        # HTTP client never called
        client._http_client.post.assert_not_called()
        # Return True so the caller's analysis pipeline proceeds as if
        # the alert was successfully sent — the cooldown / log flow continues.
        assert result is True

    async def test_debug_mode_returns_true_without_http_client(
        self, sample_request
    ):
        """Even if connect() was skipped, the guard should short-circuit
        before the None-check on _http_client."""
        debug_settings = AppSettings(
            email_api_url="http://httpwebserver:8080/EmailNotify",
            email_api_timeout=5,
            debug_read_only=True,
        )
        client = EmailAlertClient(debug_settings)
        # _http_client stays None
        result = await client.send_alert(sample_request)
        assert result is True

    async def test_normal_mode_still_posts(self, sample_request):
        """Safety net: the opposite branch (debug_read_only=False) must
        still POST. If the guard logic swaps by mistake, this fails."""
        normal_settings = AppSettings(
            email_api_url="http://httpwebserver:8080/EmailNotify",
            email_api_timeout=5,
            debug_read_only=False,
        )
        client = EmailAlertClient(normal_settings)
        client._http_client = AsyncMock()
        client._http_client.post.return_value = _mock_response(
            200, {"result": "Success", "message": ""}
        )
        await client.send_alert(sample_request)
        client._http_client.post.assert_called_once()


@pytest.mark.unit
class TestEmailOutbox:
    """v6 P1-3: every failed send must be appended to the bounded
    in-memory outbox so the operator can audit recent losses via the
    /admin/email-outbox endpoint."""

    async def test_outbox_initially_empty(self, settings):
        client = EmailAlertClient(settings)
        assert client.get_outbox_snapshot() == []
        assert client.outbox_max_size == 1000

    async def test_outbox_records_timeout_failure(self, settings, sample_request):
        client = EmailAlertClient(settings)
        client._http_client = AsyncMock()
        client._http_client.post.side_effect = httpx.TimeoutException("slow")
        await client.send_alert(sample_request)
        snapshot = client.get_outbox_snapshot()
        assert len(snapshot) == 1
        assert snapshot[0]["reason"] == "timeout"
        assert "payload" in snapshot[0]
        assert "ts" in snapshot[0]

    async def test_outbox_records_connect_error(self, settings, sample_request):
        client = EmailAlertClient(settings)
        client._http_client = AsyncMock()
        client._http_client.post.side_effect = httpx.ConnectError("dead")
        await client.send_alert(sample_request)
        snapshot = client.get_outbox_snapshot()
        assert len(snapshot) == 1
        assert snapshot[0]["reason"] == "connect_error"

    async def test_outbox_records_app_failure(self, settings, sample_request):
        """Akka returns result=Fail — counts as a failure for outbox."""
        client = EmailAlertClient(settings)
        client._http_client = AsyncMock()
        client._http_client.post.return_value = _mock_response(
            200, {"result": "Fail", "message": "queue full"}
        )
        await client.send_alert(sample_request)
        snapshot = client.get_outbox_snapshot()
        assert len(snapshot) == 1
        assert snapshot[0]["reason"] == "app_failure"
        assert "queue full" in snapshot[0]["detail"]

    async def test_outbox_does_not_record_success(
        self, settings, sample_request
    ):
        client = EmailAlertClient(settings)
        client._http_client = AsyncMock()
        client._http_client.post.return_value = _mock_response(
            200, {"result": "Success", "message": ""}
        )
        await client.send_alert(sample_request)
        assert client.get_outbox_snapshot() == []

    async def test_outbox_bounded_evicts_oldest(
        self, settings, sample_request
    ):
        """1001 failures: the first one must be evicted (LRU)."""
        client = EmailAlertClient(settings)
        client._http_client = AsyncMock()
        client._http_client.post.side_effect = httpx.TimeoutException("slow")

        # Tag each request payload with an index so we can detect eviction
        for i in range(1001):
            req = EmailAlertRequest(
                hostname=f"H{i}",
                ip="10.0.0.1",
                app="ARS",
                process="CVD",
                eqp_model="M",
                line="L1",
                code="C",
                subcode="S",
                variables={},
            )
            await client.send_alert(req)

        snapshot = client.get_outbox_snapshot()
        assert len(snapshot) == 1000
        # First entry should now correspond to request index 1, not 0
        assert snapshot[0]["payload"]["hostname"] == "H1"
        assert snapshot[-1]["payload"]["hostname"] == "H1000"

    async def test_outbox_skipped_in_debug_mode(self, sample_request):
        """Debug instances must NOT pollute the outbox."""
        debug_settings = AppSettings(
            email_api_url="http://httpwebserver:8080/EmailNotify",
            email_api_timeout=5,
            debug_read_only=True,
        )
        client = EmailAlertClient(debug_settings)
        # send_alert returns True without touching the http client
        await client.send_alert(sample_request)
        assert client.get_outbox_snapshot() == []
