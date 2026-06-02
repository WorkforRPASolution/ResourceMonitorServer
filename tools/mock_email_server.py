"""Mock ``/EmailNotify`` HTTP 서버 (의존성 0 — stdlib 만 사용).

RMS 의 ``EmailAlertClient`` 가 보내는 메일 payload 를 받아 콘솔에 예쁘게
출력하고, Akka 와 동일하게 ``{"result":"Success"}`` 를 돌려준다.
실제 메일은 발송되지 않지만, "config → 발송" 흐름을 그대로 재현한다.

실행::

    python3 tools/mock_email_server.py --port 18080

이 파일은 얇은 I/O 층이다 — 포맷팅/응답 본문 로직은 모두
``tools/email_mock.py`` (단위 테스트 대상) 에 있다.
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer

# 스크립트로 직접 실행해도(`python3 tools/mock_email_server.py`) import 되도록
# 프로젝트 루트를 sys.path 에 추가.
if __package__ in (None, ""):
    import os
    import sys

    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.email_mock import format_received, success_response_body  # noqa: E402


class _EmailNotifyHandler(BaseHTTPRequestHandler):
    # 클래스 변수 — 수신 카운터
    seq = 0

    def _write_json(self, status: int, body: dict) -> None:
        raw = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_HEAD(self) -> None:  # noqa: N802 (stdlib 명명 규약)
        # EmailAlertClient.connect() 의 startup health check 가 HEAD 를 보낸다.
        # 200 이면 "서버 도달 가능" 으로 통과된다.
        self.send_response(200)
        self.end_headers()

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b""
        try:
            payload = json.loads(raw.decode("utf-8")) if raw else {}
        except json.JSONDecodeError:
            self._write_json(400, {"result": "Fail", "message": "invalid json"})
            return

        type(self).seq += 1
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        print(format_received(payload, seq=type(self).seq, ts=ts), flush=True)

        self._write_json(200, success_response_body())

    def log_message(self, fmt: str, *args) -> None:
        # 기본 BaseHTTPRequestHandler 액세스 로그 억제 (콘솔 깔끔하게)
        return


def main() -> None:
    parser = argparse.ArgumentParser(description="Mock /EmailNotify server")
    parser.add_argument("--port", type=int, default=18080, help="listen port (default 18080)")
    parser.add_argument("--host", default="127.0.0.1", help="bind host (default 127.0.0.1)")
    args = parser.parse_args()

    server = HTTPServer((args.host, args.port), _EmailNotifyHandler)
    url = f"http://{args.host}:{args.port}/EmailNotify"
    print(f"listening on {url}  (Ctrl+C 로 종료)", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print(f"\n종료. 총 {_EmailNotifyHandler.seq}건 수신.", flush=True)
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
