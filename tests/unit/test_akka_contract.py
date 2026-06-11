"""Cross-language wire contract for POST /EmailNotify (Akka EmailHttpDataFormat).

This is the **RMS side** of a cross-codebase contract test (tdd-plan P4-계약):

- ``tests/data/akka_email_contract.json`` is the committed, language-neutral
  wire sample. RMS is the *source of truth* — ``EmailAlertRequest.to_payload()``
  must reproduce it exactly (key names, camelCase ``renderedBody``, ``model``
  not ``eqp_model``, conditional presence of ``renderedBody``/``title``).
- Akka's ``EmailHttpDataFormatSpec`` (P5-1) ``extract()``s the **same** fixture
  into its ``EmailHttpDataFormat`` case class, proving both ends agree on the
  field names/shape without a live Akka server.

If a key is renamed or its casing drifts on either side, one of these tests
fails first, forcing the fixture (and therefore the other codebase) to be
updated in lockstep.

The fixture intentionally carries **two** payloads:

- ``rendered``: full Option C payload (9 legacy fields + ``renderedBody`` +
  ``title``) — Akka extracts ``Some(value)`` for both.
- ``legacy``: dark-launch/off payload (exactly the 9 legacy fields) — Akka
  extracts ``None`` for both optional fields (true backward-compat guard).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.alert.models import EmailAlertRequest

pytestmark = [pytest.mark.unit]

_CONTRACT_PATH = Path(__file__).resolve().parents[1] / "data" / "akka_email_contract.json"

# Canonical wire values, authored once here. The committed fixture MUST match
# what RMS produces from these — the test is the source of truth, the fixture
# is the shared artifact Akka also parses.
_VARIABLES = {
    "Severity": "CRITICAL",
    "Category": "CPU",
    "MetricName": "cpu.max",
    "CurrentValue": "96.5",
    "Threshold": "95.0",
    "WindowMin": "10",
    "GrafanaUrl": "",
}
_RENDERED_BODY = (
    "<h3>[CPU] CRITICAL 임계 초과</h3>"
    "<table border=\"1\"><tr><th>장비</th><th>현재값</th></tr>"
    "<tr><td>EQP001</td><td>96.5</td></tr></table>"
)
_TITLE = "[EARS] CPU CRITICAL - EQP001"

_LEGACY_KEYS = {
    "hostname", "ip", "app", "process", "model", "line", "code", "subcode", "variables",
}


def _rendered_request() -> EmailAlertRequest:
    return EmailAlertRequest(
        hostname="EQP001",
        ip="10.0.0.99",
        app="ARS",
        process="PHOTO",
        eqp_model="MODEL-A",
        line="L1",
        code="RESOURCE_MONITOR",
        subcode="CPU_CRITICAL",
        variables=dict(_VARIABLES),
        rendered_body=_RENDERED_BODY,
        title=_TITLE,
    )


def _legacy_request() -> EmailAlertRequest:
    return EmailAlertRequest(
        hostname="EQP001",
        ip="10.0.0.99",
        app="ARS",
        process="PHOTO",
        eqp_model="MODEL-A",
        line="L1",
        code="RESOURCE_MONITOR",
        subcode="CPU_CRITICAL",
        variables=dict(_VARIABLES),
    )


def _load_contract() -> dict:
    return json.loads(_CONTRACT_PATH.read_text(encoding="utf-8"))


class TestAkkaWireContract:
    def test_contract_fixture_exists(self):
        assert _CONTRACT_PATH.exists(), (
            f"missing wire-contract fixture {_CONTRACT_PATH} — RMS to_payload() "
            "and Akka EmailHttpDataFormatSpec both consume it"
        )

    def test_rendered_payload_matches_contract(self):
        """RMS source of truth: the rendered payload reproduces the fixture exactly."""
        contract = _load_contract()
        assert _rendered_request().to_payload() == contract["rendered"]

    def test_legacy_payload_matches_contract(self):
        """Off/dark-launch payload reproduces the legacy fixture (9 fields, no
        renderedBody/title)."""
        contract = _load_contract()
        legacy = _legacy_request().to_payload()
        assert legacy == contract["legacy"]
        assert set(legacy.keys()) == _LEGACY_KEYS

    def test_rendered_fixture_has_optional_fields(self):
        """The fixture Akka parses must carry both optional fields (Some, Some)."""
        contract = _load_contract()
        rendered = contract["rendered"]
        assert rendered["renderedBody"] == _RENDERED_BODY
        assert rendered["title"] == _TITLE
        # camelCase on the wire — guards against snake_case drift
        assert "rendered_body" not in rendered
        assert rendered["model"] == "MODEL-A" and "eqp_model" not in rendered

    def test_legacy_fixture_omits_optional_fields(self):
        """The legacy fixture must NOT carry renderedBody/title → Akka gets None."""
        contract = _load_contract()
        assert "renderedBody" not in contract["legacy"]
        assert "title" not in contract["legacy"]

    def test_variables_values_are_strings(self):
        """``variables`` is ``Map[String,String]`` on the Akka side — every value
        must be a JSON string. A stray int/float/null here would extract-fail
        json4s. Guards against future drift in either codebase."""
        contract = _load_contract()
        for case in ("rendered", "legacy"):
            values = contract[case]["variables"].values()
            assert all(isinstance(v, str) for v in values), (
                f"{case}.variables must be all strings (Akka Map[String,String])"
            )

    def test_fixture_roundtrips_through_model(self):
        """Both fixtures parse back into EmailAlertRequest and re-serialize
        identically — the contract is symmetric (parse tolerates camelCase).

        This is the *fixture-integrity* direction (fixture→model→payload), the
        complement to :meth:`test_rendered_payload_matches_contract` (the
        source-of-truth RMS→fixture direction). Both are required: together they
        make the fixture and ``to_payload()`` move in lockstep. The reverse
        consumer side (fixture→Akka case class) is guarded by P5's
        ``EmailHttpDataFormatSpec``."""
        contract = _load_contract()
        assert EmailAlertRequest.model_validate(contract["rendered"]).to_payload() == contract["rendered"]
        assert EmailAlertRequest.model_validate(contract["legacy"]).to_payload() == contract["legacy"]
