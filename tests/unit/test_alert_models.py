"""Tests for src.alert.models (EmailAlertRequest)."""
import pytest

from src.alert.models import EmailAlertRequest


@pytest.mark.unit
class TestEmailAlertRequest:
    def test_required_fields(self):
        req = EmailAlertRequest(
            hostname="HOST01",
            ip="10.0.0.1",
            app="ARS",
            process="CVD",
            eqp_model="ABC123",
            line="LINE1",
            code="RESOURCE_MONITOR",
            subcode="WARNING",
            variables={"METRIC": "cpu", "VALUE": "85.2"},
        )
        assert req.hostname == "HOST01"
        assert req.app == "ARS"
        assert req.variables["METRIC"] == "cpu"

    def test_eqp_model_serializes_as_model_field(self):
        """Akka HttpWebServer expects `model` in the JSON payload."""
        req = EmailAlertRequest(
            hostname="HOST01",
            ip="10.0.0.1",
            app="ARS",
            process="CVD",
            eqp_model="ABC123",
            line="LINE1",
            code="RESOURCE_MONITOR",
            subcode="WARNING",
            variables={},
        )
        payload = req.to_payload()
        assert payload["model"] == "ABC123"
        assert "eqp_model" not in payload

    def test_app_field_serializes_in_payload(self):
        """Akka's EmailHttpDataFormat requires `app` — without it the
        json4s extract throws MappingException and the alert is dropped."""
        req = EmailAlertRequest(
            hostname="H", ip="1.1.1.1", app="ARS",
            process="P", eqp_model="M", line="L", code="C", subcode="S",
            variables={},
        )
        payload = req.to_payload()
        assert payload["app"] == "ARS"

    def test_payload_keys_match_akka_schema(self):
        """All keys expected by Akka HttpWebServer /EmailNotify."""
        req = EmailAlertRequest(
            hostname="H",
            ip="1.1.1.1",
            app="ARS",
            process="P",
            eqp_model="M",
            line="L",
            code="C",
            subcode="S",
            variables={"K": "V"},
        )
        payload = req.to_payload()
        expected_keys = {
            "hostname", "ip", "app", "process", "model", "line",
            "code", "subcode", "variables",
        }
        assert set(payload.keys()) == expected_keys

    def test_empty_variables_is_allowed(self):
        req = EmailAlertRequest(
            hostname="H", ip="1.1.1.1", app="ARS", process="P",
            eqp_model="M", line="L", code="C", subcode="S",
            variables={},
        )
        assert req.variables == {}
