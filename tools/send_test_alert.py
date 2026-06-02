"""``.env`` config 를 읽어 mock ``/EmailNotify`` 서버로 샘플 알림을 발송한다.

운영 ``EmailAlertClient`` 를 그대로 사용하므로, 실제 분석 엔진이 알림을
보내는 경로와 동일하다. ``.env`` 의 ``MONITOR_EMAIL_*`` 값이 그대로 반영된다.

실행 (RMS venv 로)::

    .venv/bin/python tools/send_test_alert.py --scenario all

먼저 다른 터미널에서 mock 서버를 띄워둘 것::

    python3 tools/mock_email_server.py --port 18080

이 파일은 얇은 I/O 층이다 — payload 생성 로직은
``tools/alert_scenarios.py`` (단위 테스트 대상) 에 있다.
"""
from __future__ import annotations

import argparse
import asyncio

# 스크립트로 직접 실행해도 import 되도록 프로젝트 루트를 sys.path 에 추가.
if __package__ in (None, ""):
    import os
    import sys

    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.alert.email_client import EmailAlertClient  # noqa: E402
from src.config.settings import get_settings  # noqa: E402
from tools.alert_scenarios import build_alert  # noqa: E402

_ALL_SCENARIOS = ["cpu", "disk", "process"]


async def _run(scenarios: list[str]) -> int:
    settings = get_settings()

    print(f"email_api_url   = {settings.email_api_url}")
    print(f"email_app_name  = {settings.email_app_name}")
    print(f"debug_read_only = {settings.debug_read_only}")
    print("-" * 60)

    if settings.debug_read_only:
        print(
            "⚠️  MONITOR_DEBUG_READ_ONLY=true 이면 HTTP POST 가 suppress 됩니다.\n"
            "    mock 서버로 메일이 가지 않습니다. .env 에서 false 로 두고 다시 실행하세요."
        )
        return 1

    client = EmailAlertClient(settings)
    try:
        await client.connect()
    except Exception as e:  # noqa: BLE001
        print(f"❌ connect 실패: {e}")
        print("   mock 서버가 떠 있는지, MONITOR_EMAIL_API_URL 이 맞는지 확인하세요.")
        return 1

    sent_ok = 0
    try:
        for scenario in scenarios:
            req = build_alert(scenario, app_name=settings.email_app_name)
            ok = await client.send_alert(req)
            status = "✅ True" if ok else "❌ False"
            print(f"  [{scenario:<7}] send_alert → {status}  (subcode={req.subcode})")
            sent_ok += int(ok)
    finally:
        await client.close()

    print("-" * 60)
    print(f"완료: {sent_ok}/{len(scenarios)} 성공")
    return 0 if sent_ok == len(scenarios) else 1


def main() -> None:
    parser = argparse.ArgumentParser(description="Send sample alerts to mock email server")
    parser.add_argument(
        "--scenario",
        choices=[*_ALL_SCENARIOS, "all"],
        default="all",
        help="발송할 시나리오 (default: all)",
    )
    args = parser.parse_args()

    scenarios = _ALL_SCENARIOS if args.scenario == "all" else [args.scenario]
    raise SystemExit(asyncio.run(_run(scenarios)))


if __name__ == "__main__":
    main()
