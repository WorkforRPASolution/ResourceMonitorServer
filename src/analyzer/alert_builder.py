"""Build EmailAlertRequest from v2 rule breaches.

In v2 the alert category comes straight from the breach (``measure.category``),
not from heuristic field-name sniffing — this fixes the latent cpu/memory
``total_used_pct`` collision (P7). The email ``code``/``subcode`` come from the
rule's resolved notify channel: ``code = notify.email_code`` and
``subcode = notify.email_subcode or "{CATEGORY}_{SEVERITY}"``.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

import structlog

from src.alert.body_renderer import (
    DEFAULT_BODY,
    order_rows,
    render_body,
    render_title,
)
from src.alert.models import EmailAlertRequest
from src.config.settings import AppSettings

if TYPE_CHECKING:
    from src.analyzer.threshold import ThresholdBreach
    from src.db.models import GroupBy, NotifyChannel

logger = structlog.get_logger(__name__)
_KST = ZoneInfo("Asia/Seoul")
# Default overflow row for the ERB cap; assumes a table-based block (the common
# case). Per-template overflow text was intentionally dropped (minimal schema).
_DEFAULT_OVERFLOW = '<tr><td colspan="99">외 @RemainingCount대 더 있습니다…</td></tr>'

# the cooldown key tuple shape, single source of truth shared with the engine
# and AlertCooldownManager (process, eqpId, proc, notify, severity). In group
# send mode the 2nd slot carries a group identifier instead of an eqpId.
CooldownKey = tuple[str, str, str, str, str]


def make_cooldown_key(
    process: str,
    breach: ThresholdBreach,
    notify_name: str,
    group_value: str | None = None,
) -> CooldownKey:
    """Build the 5-tuple cooldown identity for a breach (matches the v2 Redis
    key: ``{prefix}:cooldown:{process}:{eqp}:{proc}:{notify}:{severity}``).

    ``group_value`` replaces the eqp slot when a notify channel groups its
    sends (``model``/``process``), so one cooldown covers the whole group.
    ``None`` (the default = per-equipment) keeps the eqpId — fully backward
    compatible."""
    return (process, group_value or breach.eqp_id, breach.proc, notify_name, breach.severity)


def resolve_group_value(
    group_by: GroupBy, breach: ThresholdBreach, eqp_info: dict[str, Any], process: str
) -> str:
    """The (raw) group identifier for a breach under a channel's ``group_by``.

    ``eqp`` → the eqpId (per-equipment, current behaviour). ``model`` → the
    eqpModel. ``process`` → the process name. This is the value the cooldown
    key uses for the group and (for group sends) the title headline
    (``displayId``)."""
    if group_by == "model":
        return eqp_info.get("eqpModel", "")
    if group_by == "process":
        return process
    return breach.eqp_id


def build_email_category(
    process: str, group_by: GroupBy, eqp_model: str, email_group: str | None
) -> str | None:
    """Compose the recipient category RMS sends directly to Akka.

    Format ``EMAIL-{process}-{model_token}-{email_group}`` where ``model_token``
    is ``"ALL"`` for ``process`` grouping (models are mixed, no single model) and
    the representative equipment's eqpModel otherwise (``model``/``eqp``). Returns
    ``None`` when ``email_group`` is unset/empty — the channel then falls back to
    Akka's ``getEmailCategory`` derivation. See
    docs/rms-email-group-routing-decision-2026-06-14.md §3.1."""
    if not email_group:
        return None
    model_token = "ALL" if group_by == "process" else eqp_model
    return f"EMAIL-{process}-{model_token}-{email_group}"


def resolve_code_subcode(notify: NotifyChannel, breach: ThresholdBreach) -> tuple[str, str]:
    """The email ``(code, subcode)`` for a breach — single source shared by the
    request builder and the engine's template lookup (so they never diverge)."""
    code = notify.email_code
    subcode = notify.email_subcode or f"{breach.category.upper()}_{breach.severity}"
    return code, subcode


def build_alert_request(
    breach: ThresholdBreach,
    eqp_info: dict[str, Any],
    process: str,
    settings: AppSettings,
    notify: NotifyChannel,
    window_minutes: int,
    affected_equipment: list[str] | None = None,
    *,
    members: list[ThresholdBreach] | None = None,
    eqp_lookup: dict[str, dict[str, Any]] | None = None,
    timestamp: datetime | None = None,
    template: dict[str, Any] | None = None,
    email_category: str | None = None,
    display_id: str | None = None,
) -> EmailAlertRequest:
    """Construct an EmailAlertRequest from a breach + equipment info + channel.

    ``affected_equipment`` (group send only) adds ``AffectedEquipment`` /
    ``AffectedCount`` variables listing every equipment in the group. When
    ``None`` (per-equipment send) the variable set is unchanged.

    Option C (when ``settings.rms_custom_body_enabled``): also renders a custom
    HTML body + subject into ``renderedBody``/``title`` using ``members`` (per-eqp
    rows), ``eqp_lookup`` (row metadata), ``timestamp`` (@Timestamp), and
    ``template`` (the fetched RESOURCE_MONITOR_EMAIL_TEMPLATE doc, or the built-in
    default when None). This function stays **synchronous** — the async template
    fetch happens in the engine's ``_dispatch``."""
    category = breach.category.upper()
    code, subcode = resolve_code_subcode(notify, breach)
    grafana_url = ""
    if settings.grafana_base_url and settings.grafana_dashboard_uid:
        grafana_url = (
            f"{settings.grafana_base_url}/d/{settings.grafana_dashboard_uid}"
            f"?var-eqpId={breach.eqp_id}&var-process={process}"
        )

    variables = {
        "Severity": breach.severity,
        "Category": category,
        "MetricName": breach.fact,
        "CurrentValue": str(breach.current_value),
        "Threshold": str(breach.threshold_value),
        "WindowMin": str(window_minutes),
        "GrafanaUrl": grafana_url,
    }
    if affected_equipment is not None:
        variables["AffectedEquipment"] = ", ".join(affected_equipment)
        variables["AffectedCount"] = str(len(affected_equipment))

    rendered_body: str | None = None
    title: str | None = None
    if getattr(settings, "rms_custom_body_enabled", False):
        scalars = _build_scalars(
            breach, eqp_info, process, notify, window_minutes,
            affected_equipment, timestamp, code, subcode, category, grafana_url,
        )
        rows = _build_rows(breach, members, eqp_lookup or {})
        rendered_body, title = _render_custom_body(template, scalars, rows, settings, code, subcode)

    return EmailAlertRequest(
        # hostname=eqpId: Akka HttpWebServer는 EmailHttpDataFormat.hostname을
        # eqpId로 취급(getEmailCategory/getSdwt가 EQP_INFO를 eqpId로 조회,
        # @Hostname 치환·메일 제목). PRD §장비 ID 명세와 일치. localpc(PC명) 아님.
        hostname=breach.eqp_id,
        ip=eqp_info.get("ipAddr", ""),
        app=settings.email_app_name,
        process=process,
        eqp_model=eqp_info.get("eqpModel", ""),
        line=eqp_info.get("line", ""),
        code=code,
        subcode=subcode,
        variables=variables,
        rendered_body=rendered_body,
        title=title,
        email_category=email_category,
        display_id=display_id,
    )


def _fmt_timestamp(ts: datetime | None) -> str | None:
    """Format @Timestamp as ``YYYY-MM-DD HH:MM KST`` (Asia/Seoul). Naive datetimes
    are assumed UTC."""
    if ts is None:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(_KST).strftime("%Y-%m-%d %H:%M") + " KST"


def _build_scalars(
    breach: ThresholdBreach, eqp_info: dict[str, Any], process: str,
    notify: NotifyChannel, window_minutes: int, affected_equipment: list[str] | None,
    timestamp: datetime | None, code: str, subcode: str, category: str, grafana_url: str,
) -> dict[str, Any]:
    """Email-level @-token values (raw; the renderer formats/escapes)."""
    return {
        "@Severity": breach.severity,
        "@Category": category,
        "@Fact": breach.fact,
        "@CurrentValue": breach.current_value,
        "@Threshold": breach.threshold_value,
        "@Operator": breach.op,
        "@WindowMin": window_minutes,
        "@Timestamp": _fmt_timestamp(timestamp),
        "@Process": process,
        "@GroupBy": notify.group_by,
        "@GroupValue": resolve_group_value(notify.group_by, breach, eqp_info, process),
        "@AffectedCount": len(affected_equipment) if affected_equipment is not None else None,
        "@AffectedEquipment": ", ".join(affected_equipment) if affected_equipment else None,
        "@GrafanaUrl": grafana_url,
        "@Hostname": breach.eqp_id,
        "@Model": eqp_info.get("eqpModel", ""),
        "@Line": eqp_info.get("line", ""),
        "@IP": eqp_info.get("ipAddr", ""),
        "@CODE": f"{code}-{subcode}" if subcode else code,
    }


def _build_rows(
    breach: ThresholdBreach,
    members: list[ThresholdBreach] | None,
    eqp_lookup: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """Per-equipment @Row.* values, ordered (severity desc → value worst → eqpId)."""
    breaches = members if members else [breach]
    rows = []
    for m in breaches:
        info = eqp_lookup.get(m.eqp_id, {})
        rows.append({
            "@Row.EqpId": m.eqp_id,
            "@Row.CurrentValue": m.current_value,
            "@Row.Threshold": m.threshold_value,
            "@Row.Severity": m.severity,
            "@Row.Fact": m.fact,
            "@Row.Category": m.category.upper(),
            "@Row.Operator": m.op,
            "@Row.Model": info.get("eqpModel", ""),
            "@Row.Line": info.get("line", ""),
            "@Row.IP": info.get("ipAddr", ""),
            "@Row.Proc": m.proc,
        })
    return order_rows(rows)


def _render_custom_body(
    template: dict[str, Any] | None, scalars: dict[str, Any],
    rows: list[dict[str, Any]], settings: AppSettings, code: str, subcode: str,
) -> tuple[str, str]:
    """Render (body, title) from the template, falling back to the built-in
    default body/title on a missing template or any render error (D5)."""
    html_template = (template.get("html") if template else None) or DEFAULT_BODY
    title_template = template.get("title", "") if template else ""
    try:
        body = render_body(
            html_template, scalars, rows,
            row_limit=settings.rms_erb_row_limit,
            overflow_text=_DEFAULT_OVERFLOW,
            byte_cap=settings.rms_body_byte_cap,
        )
        title = render_title(title_template, scalars)
    except Exception:
        logger.warning("rms_body_render_failed", code=code, subcode=subcode)
        body = render_body(
            DEFAULT_BODY, scalars, rows,
            row_limit=settings.rms_erb_row_limit,
            overflow_text=_DEFAULT_OVERFLOW,
            byte_cap=settings.rms_body_byte_cap,
        )
        title = render_title("", scalars)
    return body, title
