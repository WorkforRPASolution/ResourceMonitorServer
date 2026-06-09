"""Pure renderer for the RMS alert-email body (Option C).

Renders an operator-authored HTML template (the ``html`` column of
``RESOURCE_MONITOR_EMAIL_TEMPLATE``) into finished HTML that RMS ships to Akka as
the ``renderedBody`` field. The renderer:

1. expands the Equipment Repeat Block (ERB) once per equipment row,
2. substitutes ``@``-tokens with **context-aware escaping** (text vs url),
3. leaves unknown tokens literal and renders known-but-missing tokens blank.

Design decisions (architecture §7.2-C, D6):

- Numbers render as ``str(value)`` verbatim (no reformatting); ``None`` → ``-``;
  str/trend thresholds verbatim. The renderer receives **raw** values, not
  pre-stringified ones, so the ``None`` branch stays live.
- Text tokens are HTML-escaped (``& < > " '``). The single url token
  (``@GrafanaUrl``) is scheme-validated and quote-escaped, but query ``&`` is
  preserved so the deep link stays valid.

This module is the **canonical** renderer; the WebManager preview mirrors it via
a shared golden fixture (see tdd-plan P6-3).
"""
from __future__ import annotations

import html
import re
from typing import Any

import structlog

from src.alert.tokens import TOKEN_CONTEXT

logger = structlog.get_logger(__name__)

_TRUNCATE_MARKER = "<!-- truncated -->"

# Infra token Akka still substitutes inside renderedBody (D2). A literal copy
# inside a data value is neutralized (``@`` → ``&#64;``, renders as ``@``) so
# Akka's post-substitution can't mangle equipment/metric values.
_RESERVED_AKKA_TOKEN = "@HttpWebServerAddress"
_RESERVED_AKKA_NEUTRAL = "&#64;HttpWebServerAddress"

# Built-in default subject, used when a template's title is empty (D1, D5). Must
# be colon-free (EmailActor splits "title:body" on the first ':').
DEFAULT_TITLE = "[EARS] 자원 모니터링 알림"

# Built-in default body, used when no RESOURCE_MONITOR_EMAIL_TEMPLATE row matches
# or rendering errors (D5) — guarantees a send never fails for "no template". Uses
# only standard tokens + an ERB so single and group modes both render.
DEFAULT_BODY = (
    "<h3>[@Category] @Severity 임계 초과</h3>"
    "<p>공정 @Process · 최근 @WindowMin분 · @Timestamp</p>"
    "<p>영향 장비 @AffectedCount대</p>"
    "<table border=\"1\" cellpadding=\"4\">"
    "<tr><th>장비</th><th>지표</th><th>현재값</th><th>임계</th><th>심각도</th></tr>"
    "<!--@EachEquipment-->"
    "<tr><td>@Row.EqpId</td><td>@Row.Fact</td><td>@Row.CurrentValue</td>"
    "<td>@Row.Threshold</td><td>@Row.Severity</td></tr>"
    "<!--@EndEachEquipment-->"
    "</table>"
)

# Matches an @-token: an optional ``Row.`` namespace then an alpha identifier.
# Greedy ``[A-Za-z]+`` means ``@ThresholdX`` matches as a whole (and is therefore
# treated as unknown), so it never collides with the ``@Threshold`` prefix.
_TOKEN_RE = re.compile(r"@(?:Row\.)?[A-Za-z]+")

# Equipment Repeat Block (ERB) markers — HTML comments so TinyMCE-rendered
# templates keep them invisible (architecture §7.3).
ERB_START = "<!--@EachEquipment-->"
ERB_END = "<!--@EndEachEquipment-->"

# severity ordering for order_rows: lower rank sorts first (CRITICAL before WARNING)
_SEV_RANK = {"CRITICAL": 0, "WARNING": 1}


def _format_value(raw: Any) -> str:
    """Stringify a raw value per the decided rule: ``None`` → ``-``, else
    ``str(raw)`` verbatim (no float reformatting; str/trend pass through)."""
    if raw is None:
        return "-"
    return str(raw)


def _escape_text(raw: Any) -> str:
    escaped = html.escape(_format_value(raw), quote=True)
    return escaped.replace(_RESERVED_AKKA_TOKEN, _RESERVED_AKKA_NEUTRAL)


def _escape_url(raw: Any) -> str:
    """Scheme-validate an RMS-built URL and attribute-escape quotes only.

    Non-http(s) values (e.g. ``javascript:``) render blank. Query ``&`` is kept
    so ``?var-a=1&var-b=2`` deep links are not broken (D6, §5-②)."""
    if not raw:
        return ""
    s = str(raw)
    if not (s.startswith("http://") or s.startswith("https://")):
        return ""
    return s.replace('"', "&quot;")


def _render_token(token: str, value_map: dict[str, Any]) -> str:
    """Render one known token. Known-but-missing → blank."""
    if token not in value_map:
        return ""
    if TOKEN_CONTEXT[token] == "url":
        return _escape_url(value_map[token])
    return _escape_text(value_map[token])


def _substitute(
    text: str, value_map: dict[str, Any], *, only_provided: bool = False
) -> str:
    """Substitute known @-tokens; leave unknown tokens literal.

    With ``only_provided`` (the per-row pass), substitute *only* tokens present in
    ``value_map`` and leave other known tokens (e.g. scalars inside the ERB) for
    the later scalar pass."""
    def repl(m: re.Match[str]) -> str:
        token = m.group(0)
        if token not in TOKEN_CONTEXT:  # unknown token → literal (lint-guarded)
            return token
        if only_provided and token not in value_map:
            return token  # defer to the scalar pass
        return _render_token(token, value_map)

    return _TOKEN_RE.sub(repl, text)


def _expand_erb(
    template: str,
    rows: list[dict[str, Any]],
    row_limit: int | None,
    overflow_text: str,
) -> str:
    """Expand the single Equipment Repeat Block once per row (row tokens only).

    Caps rows at ``row_limit`` (None = no cap); dropped rows append
    ``overflow_text`` with ``@RemainingCount`` substituted. Scalar tokens inside
    the block are left for the scalar pass. No ERB markers → returned unchanged."""
    if ERB_START not in template:
        return template
    pre, rest = template.split(ERB_START, 1)
    inner, post = rest.split(ERB_END, 1)
    capped = rows if row_limit is None else rows[:row_limit]
    remaining = len(rows) - len(capped)
    pieces = []
    for i, row in enumerate(capped, start=1):
        row_map = {**row, "@Row.Index": i}
        pieces.append(_substitute(inner, row_map, only_provided=True))
    overflow = overflow_text.replace("@RemainingCount", str(remaining)) if remaining else ""
    return pre + "".join(pieces) + overflow + post


def _truncate_to_bytes(text: str, byte_cap: int) -> str:
    """Best-effort truncate to ``byte_cap`` UTF-8 bytes at a safe boundary,
    appending a marker. Last-resort backstop for unknown Redis/ESB body limits."""
    budget = max(0, byte_cap - len(_TRUNCATE_MARKER.encode("utf-8")))
    truncated = text.encode("utf-8")[:budget].decode("utf-8", errors="ignore")
    return truncated + _TRUNCATE_MARKER


def order_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Deterministic default ordering: severity desc → current value worst →
    eqpId asc. ``worst`` defaults to highest value first (high-side threshold
    assumption); rows with no numeric value sort last within their severity."""
    def key(row: dict[str, Any]) -> tuple[int, float, str]:
        sev = _SEV_RANK.get(str(row.get("@Row.Severity", "")).upper(), 99)
        cv = row.get("@Row.CurrentValue")
        neg_value = -float(cv) if isinstance(cv, (int, float)) else float("inf")
        return (sev, neg_value, str(row.get("@Row.EqpId", "")))

    return sorted(rows, key=key)


def render_title(title_template: str, scalars: dict[str, Any]) -> str:
    """Render the email subject (plain text, NOT HTML-escaped).

    Falls back to :data:`DEFAULT_TITLE` when the template is empty (RMS always
    supplies a title in renderedBody mode — there is no Akka-side fallback, D1),
    and strips ``:`` so the downstream ``title:body`` first-colon split is safe."""
    tpl = title_template or DEFAULT_TITLE

    def repl(m: re.Match[str]) -> str:
        token = m.group(0)
        if token not in TOKEN_CONTEXT:
            return token
        if token not in scalars:
            return ""
        return _format_value(scalars[token])  # plain, no HTML escape

    rendered = _TOKEN_RE.sub(repl, tpl)
    return rendered.replace(":", " ").strip()


def render_body(
    template_html: str,
    scalars: dict[str, Any],
    rows: list[dict[str, Any]] | None = None,
    *,
    row_limit: int | None = None,
    overflow_text: str = "",
    byte_cap: int | None = None,
) -> str:
    """Render the body template: expand the ERB per row, then substitute scalars.

    ``rows`` render in the given order (call :func:`order_rows` first for the
    default ordering). ``row_limit``/``overflow_text`` cap the table;
    ``byte_cap`` is a last-resort UTF-8 byte backstop. Templates without an ERB
    block ignore ``rows``."""
    expanded = _expand_erb(template_html, rows or [], row_limit, overflow_text)
    out = _substitute(expanded, scalars)
    if byte_cap is not None and len(out.encode("utf-8")) > byte_cap:
        logger.warning("rms_email_body_truncated", byte_cap=byte_cap, rendered_bytes=len(out.encode("utf-8")))
        out = _truncate_to_bytes(out, byte_cap)
    return out
