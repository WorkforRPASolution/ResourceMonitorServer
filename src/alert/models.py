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
    # Option C (additive, backward-compatible). When the custom-body feature is
    # off these stay None and are omitted from the payload, so Akka receives the
    # legacy 9 fields. When set, RMS has pre-rendered the full HTML body/subject;
    # Akka uses them directly (json4s Option[String] tolerates absence — D7/§5-④).
    rendered_body: str | None = Field(default=None, alias="renderedBody")
    title: str | None = None
    # Group send routing (additive, backward-compatible). When set, RMS has
    # pre-composed the recipient category (EMAIL-{process}-{model}-{email_group})
    # so Akka routes directly without getEmailCategory derivation; `display_id`
    # replaces the representative eqpId in the title headline (= group_value).
    # Both stay None for individual sends / unset channels → omitted from the
    # payload, so Akka receives the legacy shape and derives as before.
    email_category: str | None = Field(default=None, alias="emailCategory")
    display_id: str | None = Field(default=None, alias="displayId")

    def to_payload(self) -> dict[str, Any]:
        """Return the JSON body Akka expects, using `model` not `eqp_model`.

        `renderedBody`/`title`/`emailCategory`/`displayId` are included only when
        set, preserving the exact legacy 9-field payload in dark-launch/off mode."""
        payload: dict[str, Any] = {
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
        if self.rendered_body is not None:
            payload["renderedBody"] = self.rendered_body
        if self.title is not None:
            payload["title"] = self.title
        if self.email_category is not None:
            payload["emailCategory"] = self.email_category
        if self.display_id is not None:
            payload["displayId"] = self.display_id
        return payload
