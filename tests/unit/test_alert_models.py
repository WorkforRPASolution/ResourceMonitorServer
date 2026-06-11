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

    def test_rendered_body_title_default_none(self):
        req = EmailAlertRequest(
            hostname="H", ip="1.1.1.1", app="ARS", process="P",
            eqp_model="M", line="L", code="C", subcode="S", variables={},
        )
        assert req.rendered_body is None
        assert req.title is None

    def test_to_payload_omits_rendered_body_title_when_none(self):
        """Dark-launch / legacy: with no custom body, payload stays 9 fields."""
        req = EmailAlertRequest(
            hostname="H", ip="1.1.1.1", app="ARS", process="P",
            eqp_model="M", line="L", code="C", subcode="S", variables={"K": "V"},
        )
        payload = req.to_payload()
        assert "renderedBody" not in payload
        assert "title" not in payload
        assert set(payload.keys()) == {
            "hostname", "ip", "app", "process", "model", "line",
            "code", "subcode", "variables",
        }

    def test_to_payload_includes_rendered_body_title_when_set(self):
        req = EmailAlertRequest(
            hostname="H", ip="1.1.1.1", app="ARS", process="P",
            eqp_model="M", line="L", code="C", subcode="S", variables={},
            rendered_body="<p>x</p>", title="[EARS] t",
        )
        payload = req.to_payload()
        assert payload["renderedBody"] == "<p>x</p>"
        assert payload["title"] == "[EARS] t"

    def test_rendered_body_parses_from_camel_alias(self):
        req = EmailAlertRequest.model_validate({
            "hostname": "H", "ip": "1.1.1.1", "app": "ARS", "process": "P",
            "model": "M", "line": "L", "code": "C", "subcode": "S",
            "variables": {}, "renderedBody": "<b>y</b>",
        })
        assert req.rendered_body == "<b>y</b>"
