"""HTTP client for the Akka HttpWebServer `/EmailNotify` endpoint.

Akka's `EmailWorker` returns ``HttpResponse(result, message)`` where the
``result`` field is the literal string ``"Success"`` on the success path
(see ``EmailWorker.scala`` — both ``SendEmail`` and ``SendEmailForRTM``
emit capital-S ``"Success"``). On any application-level failure (missing
template, missing email category, parse exception) it returns
``"Fail"`` with a diagnostic message.

The comparison below is case-insensitive so we tolerate either casing —
earlier revisions of this module assumed lowercase ``"success"`` and
every send silently "failed" (email was actually delivered, but we
logged failure and never set the cooldown, leading to duplicate alerts).

v6 P1-3 — In-memory outbox:
    Failed alerts are appended to a bounded ``deque(maxlen=1000)`` so
    operators can inspect them via ``GET /admin/email-outbox``. This is
    the Phase 0 substitute for a persistent outbox/DLQ — the deque is
    NOT durable, so a pod restart loses the contents. Phase 1+ may
    upgrade to Redis LIST or disk if operationally justified.
"""
from __future__ import annotations

import time
from collections import deque
from typing import Any

import httpx
import structlog

from src.alert.models import EmailAlertRequest
from src.config.settings import AppSettings

logger = structlog.get_logger(__name__)

# Akka emits "Success" (capital S); we compare case-insensitively so a
# future Akka tweak to lowercase doesn't silently break alerting.
_SUCCESS_RESULT = "success"
# v6 P1-3 — bound on the in-memory outbox. 1000 entries × ~1KB each = ~1MB,
# negligible for any pod we'd run. LRU eviction (deque drops oldest).
_OUTBOX_MAXLEN = 1000


class EmailAlertClient:
    def __init__(self, settings: AppSettings) -> None:
        self._settings = settings
        self._api_url = settings.email_api_url
        self._http_client: httpx.AsyncClient | None = None
        # v6 P1-3 — bounded in-memory outbox of failed sends. Operators
        # can inspect this via ``GET /admin/email-outbox``. Phase 0 only.
        self._outbox: deque[dict[str, Any]] = deque(maxlen=_OUTBOX_MAXLEN)

    async def connect(self) -> None:
        """Build the httpx client and verify the Akka API is reachable.

        v6 P0-3: a startup health check was added so a typo in
        MONITOR_EMAIL_API_URL or a wrong port fails the boot loudly. We
        reuse the existing ``health_check()`` (HEAD request — Akka returns
        404/405 for HEAD, but the connection itself succeeding is enough).

        Debug Read-Only mode skips the check entirely. A debug instance is
        often run on a developer laptop without network access to the prod
        Akka box; the connect must succeed regardless because outbound
        emails are HTTP-suppressed by ``send_alert``'s debug guard.
        """
        self._http_client = httpx.AsyncClient(timeout=self._settings.email_api_timeout)
        if self._settings.debug_read_only:
            return
        if not await self.health_check():
            raise RuntimeError("email_startup_health_check_failed")

    async def close(self) -> None:
        if self._http_client is not None:
            try:
                await self._http_client.aclose()
            except Exception as e:
                logger.warning("email_client_close_failed", error=str(e))
            finally:
                self._http_client = None

    def _record_outbox_failure(
        self, request: EmailAlertRequest, reason: str, detail: str = ""
    ) -> None:
        """Append a failed-send record to the bounded outbox.

        Skipped in debug_read_only mode so a developer's local debugging
        run does not pollute the operator's view of real failures.
        """
        if self._settings.debug_read_only:
            return
        self._outbox.append(
            {
                "ts": time.time(),
                "reason": reason,
                "detail": detail[:500],
                "payload": request.to_payload(),
            }
        )

    def get_outbox_snapshot(self) -> list[dict[str, Any]]:
        """Return a list copy of current outbox entries (oldest first).

        Returned list is a snapshot — mutating it does NOT affect the deque.
        """
        return list(self._outbox)

    @property
    def outbox_max_size(self) -> int:
        return _OUTBOX_MAXLEN

    async def send_alert(self, request: EmailAlertRequest) -> bool:
        """POST the request. Return True iff Akka replies ``result == "success"``.

        Any exception (timeout, connect error, 5xx, invalid JSON) is caught
        and logged — the caller treats this as "email not sent" and handles
        the cooldown side-effect accordingly.

        Debug Read-Only mode: the HTTP POST is suppressed entirely. The full
        request payload is logged so the operator can still see WHAT would
        have been sent, and we return True so the caller's analysis flow
        proceeds as if the alert succeeded (cooldown path still runs through
        the debug-guarded cooldown manager).

        v6 P1-3: every False return also appends to ``_outbox`` so the
        operator can inspect recent losses via ``GET /admin/email-outbox``.
        """
        if self._settings.debug_read_only:
            logger.warning(
                "debug_would_send_email",
                request=request.to_payload(),
                reason="debug_read_only=true — HTTP POST suppressed",
            )
            return True
        if self._http_client is None:
            logger.error("email_send_not_connected")
            self._record_outbox_failure(request, "not_connected")
            return False
        try:
            resp = await self._http_client.post(
                self._api_url, json=request.to_payload()
            )
            resp.raise_for_status()
            data = resp.json()
        except httpx.TimeoutException as e:
            logger.warning("email_send_timeout", error=str(e))
            self._record_outbox_failure(request, "timeout", str(e))
            return False
        except httpx.ConnectError as e:
            logger.error("email_send_connect_error", error=str(e))
            self._record_outbox_failure(request, "connect_error", str(e))
            return False
        except httpx.HTTPStatusError as e:
            logger.error(
                "email_send_http_error",
                status=e.response.status_code,
                body=e.response.text[:500],
            )
            self._record_outbox_failure(
                request,
                "http_error",
                f"status={e.response.status_code} body={e.response.text[:200]}",
            )
            return False
        except (ValueError, KeyError) as e:
            logger.error("email_send_invalid_response", error=str(e))
            self._record_outbox_failure(request, "invalid_response", str(e))
            return False

        result = data.get("result", "") if isinstance(data, dict) else ""
        message = data.get("message", "") if isinstance(data, dict) else ""
        if isinstance(result, str) and result.lower() == _SUCCESS_RESULT:
            logger.info("email_send_ok", message=message)
            return True
        logger.warning(
            "email_send_app_failure", result=result, message=message
        )
        self._record_outbox_failure(
            request, "app_failure", f"result={result} message={message}"
        )
        return False

    async def health_check(self) -> bool:
        """Best-effort availability probe.

        The Akka endpoint does not expose a dedicated health route in Phase 0
        (PRD notes this as an open item). We therefore fall back to a cheap
        HEAD request on the same URL — Akka returns 404/405 for HEAD which is
        still "the server is reachable", so we treat any response as healthy
        and only exceptions as unhealthy.
        """
        if self._http_client is None:
            return False
        try:
            await self._http_client.request("HEAD", self._api_url)
            return True
        except Exception:
            return False
