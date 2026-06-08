"""Build EmailAlertRequest from v2 rule breaches.

In v2 the alert category comes straight from the breach (``measure.category``),
not from heuristic field-name sniffing — this fixes the latent cpu/memory
``total_used_pct`` collision (P7). The email ``code``/``subcode`` come from the
rule's resolved notify channel: ``code = notify.email_code`` and
``subcode = notify.email_subcode or "{CATEGORY}_{SEVERITY}"``.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from src.alert.models import EmailAlertRequest
from src.config.settings import AppSettings

if TYPE_CHECKING:
    from src.analyzer.threshold import ThresholdBreach
    from src.db.models import NotifyChannel

# the cooldown key tuple shape, single source of truth shared with the engine
# and AlertCooldownManager (process, eqpId, proc, notify, severity).
CooldownKey = tuple[str, str, str, str, str]


def make_cooldown_key(process: str, breach: ThresholdBreach, notify_name: str) -> CooldownKey:
    """Build the 5-tuple cooldown identity for a breach (matches the v2 Redis
    key: ``{prefix}:cooldown:{process}:{eqp}:{proc}:{notify}:{severity}``)."""
    return (process, breach.eqp_id, breach.proc, notify_name, breach.severity)


def build_alert_request(
    breach: ThresholdBreach,
    eqp_info: dict[str, Any],
    process: str,
    settings: AppSettings,
    notify: NotifyChannel,
    window_minutes: int,
) -> EmailAlertRequest:
    """Construct an EmailAlertRequest from a breach + equipment info + channel."""
    category = breach.category.upper()
    subcode = notify.email_subcode or f"{category}_{breach.severity}"
    grafana_url = ""
    if settings.grafana_base_url and settings.grafana_dashboard_uid:
        grafana_url = (
            f"{settings.grafana_base_url}/d/{settings.grafana_dashboard_uid}"
            f"?var-eqpId={breach.eqp_id}&var-process={process}"
        )

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
        code=notify.email_code,
        subcode=subcode,
        variables={
            "Severity": breach.severity,
            "Category": category,
            "MetricName": breach.fact,
            "CurrentValue": str(breach.current_value),
            "Threshold": str(breach.threshold_value),
            "WindowMin": str(window_minutes),
            "GrafanaUrl": grafana_url,
        },
    )
