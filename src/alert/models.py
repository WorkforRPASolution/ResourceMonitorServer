"""Email alert request model.

Payload shape matches the Akka `HttpWebServer /EmailNotify` endpoint, which
expects the `model` field (not `eqp_model`). We keep `eqp_model` as the
Python-side name and translate at the edge via `to_payload()`.

The `app` field is required by Akka's `EmailHttpDataFormat` and is used as
a key into the `EMAIL_TEMPLATE` and `EMAIL_CATEGORY` collections — omitting
it causes json4s `extract` to throw a `MappingException` and the alert is
silently dropped on the server side.
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class EmailAlertRequest(BaseModel):
    """Request body for `POST /EmailNotify`.

    The corresponding Akka case class is (simplified)::

        case class EmailHttpDataFormat(
            hostname: String, ip: String, app: String,
            process: String, model: String, line: String,
            code: String, subcode: String,
            variables: Map[String, String],
        )
    """

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    hostname: str
    ip: str
    app: str
    process: str
    eqp_model: str = Field(alias="model")
    line: str
    code: str
    subcode: str
    variables: dict[str, str]

    def to_payload(self) -> dict[str, Any]:
        """Return the JSON body Akka expects, using `model` not `eqp_model`."""
        return {
            "hostname": self.hostname,
            "ip": self.ip,
            "app": self.app,
            "process": self.process,
            "model": self.eqp_model,
            "line": self.line,
            "code": self.code,
            "subcode": self.subcode,
            "variables": self.variables,
        }
