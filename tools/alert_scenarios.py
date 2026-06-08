"""시나리오 이름 → 샘플 ``EmailAlertRequest`` 빌더.

개발 PC 에서 mock ``/EmailNotify`` 서버로 손수 발송해 볼 알림을 만든다.
운영 분석 엔진과 동일한 payload 가 나오도록, ``ThresholdBreach`` 와
``build_alert_request`` 를 그대로 재사용한다.
"""
from __future__ import annotations

from types import SimpleNamespace

from src.alert.models import EmailAlertRequest
from src.analyzer.alert_builder import build_alert_request
from src.analyzer.threshold import ThresholdBreach
from src.db.models import NotifyChannel

# scenario → (fact, current, threshold, severity, category, op)
_SCENARIOS: dict[str, tuple[str, float, float, str, str, str]] = {
    "cpu": ("cpu.max", 92.0, 80.0, "WARNING", "cpu", ">="),
    "disk": ("disk.max", 98.0, 95.0, "CRITICAL", "disk", ">="),
    "process": ("proc_required.min", 0.0, 0.0, "CRITICAL", "process_watch", "=="),
}

# 샘플 설비 정보 (EQP_INFO 에서 읽어오는 필드들과 동일한 키)
_SAMPLE_EQP_INFO = {
    "localpc": "DEV-LOCALPC-01",
    "ipAddr": "10.0.0.42",
    "eqpModel": "MODEL-DEV-ABC",
    "line": "L-DEV",
}

_WINDOW_MINUTES = 10


def build_alert(scenario: str, app_name: str) -> EmailAlertRequest:
    """``scenario`` 에 해당하는 샘플 ``EmailAlertRequest`` 를 만든다.

    :param scenario: ``"cpu" | "disk" | "process"``
    :param app_name: payload 의 ``app`` 필드 (settings.email_app_name 에서 옴)
    :raises ValueError: 알 수 없는 시나리오
    """
    if scenario not in _SCENARIOS:
        raise ValueError(
            f"unknown scenario: {scenario!r} (choices: {sorted(_SCENARIOS)})"
        )

    fact, current, threshold, severity, category, op = _SCENARIOS[scenario]
    breach = ThresholdBreach(
        eqp_id="DEV-EQP-01",
        proc="@system",
        rule_id=f"{scenario}_demo",
        fact=fact,
        category=category,
        op=op,
        current_value=current,
        threshold_value=threshold,
        severity=severity,
    )
    settings = SimpleNamespace(
        email_app_name=app_name,
        grafana_base_url="",
        grafana_dashboard_uid="",
    )
    return build_alert_request(
        breach=breach,
        eqp_info=_SAMPLE_EQP_INFO,
        process="DEV_PROC",
        settings=settings,
        notify=NotifyChannel(cooldown_minutes=_WINDOW_MINUTES),
        window_minutes=_WINDOW_MINUTES,
    )
