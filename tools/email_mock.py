"""Mock ``/EmailNotify`` 서버의 순수 로직.

네트워크 I/O 와 분리해 단위 테스트 가능하게 한 부분:
- ``success_response_body()`` — Akka 가 돌려주는 성공 응답 본문
- ``format_received(payload, seq, ts)`` — 수신한 메일 payload 를 콘솔용
  문자열로 예쁘게 포맷팅

실제 소켓/HTTP 처리는 ``tools/mock_email_server.py`` 가 담당하며, 이 모듈을
호출만 한다.
"""
from __future__ import annotations

import json
from typing import Any

# Akka EmailWorker 는 성공 시 대문자 "Success" 를 돌려준다
# (src/alert/email_client.py 의 case-insensitive 비교와 호환).
_SUCCESS_BODY = {"result": "Success", "message": ""}

# 헤더 라인에 보여줄 필드 (순서 유지)
_HEADER_FIELDS = (
    "hostname",
    "ip",
    "app",
    "process",
    "model",
    "line",
    "code",
    "subcode",
)

_MISSING = "<none>"


def success_response_body() -> dict[str, str]:
    """Akka 성공 응답과 동일한 본문을 반환한다."""
    return dict(_SUCCESS_BODY)


def format_received(payload: dict[str, Any], seq: int, ts: str) -> str:
    """수신한 메일 payload 를 콘솔 출력용 문자열로 만든다.

    :param payload: ``/EmailNotify`` 로 들어온 JSON dict
    :param seq: 수신 순번 (1부터)
    :param ts: 수신 시각 문자열
    """
    lines: list[str] = []
    lines.append("=" * 60)
    lines.append(f"📧  메일 수신  #{seq}   {ts}")
    lines.append("-" * 60)

    for field in _HEADER_FIELDS:
        value = payload.get(field, _MISSING)
        lines.append(f"  {field:<9}: {value}")

    variables = payload.get("variables", _MISSING)
    lines.append("  variables:")
    if isinstance(variables, dict):
        rendered = json.dumps(variables, indent=4, ensure_ascii=False)
        lines.append(rendered)
    else:
        lines.append(f"    {variables}")

    lines.append("=" * 60)
    return "\n".join(lines)
