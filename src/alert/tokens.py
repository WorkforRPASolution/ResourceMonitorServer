"""Token catalog for the RMS alert-email body renderer (Option C).

Single source of truth for which ``@``-tokens the renderer substitutes and the
escape *context* each one uses:

- ``text`` — substituted as HTML-escaped text content.
- ``url``  — substituted into an ``href``/``src`` attribute (scheme-validated,
  quote-escaped, query ``&`` preserved). Only ``@GrafanaUrl`` is a URL token.

Two layers:

- :data:`SCALAR_TOKENS` — email-level tokens bound from the representative
  breach + equipment info (one value per email).
- :data:`ROW_TOKENS` — ``@Row.*``-namespaced tokens bound per equipment inside
  the Equipment Repeat Block (ERB).

The WebManager editor palette/lint mirror this catalog (see editor-ui §8). The
refined ``@Metric`` token is intentionally **excluded in v1** (no data source —
``Measure`` has no display name); use ``@Fact`` for the metric identity. See
docs/resource-monitor-email-template-architecture.md §7.2.
"""
from __future__ import annotations

# token -> escape context
SCALAR_TOKENS: dict[str, str] = {
    "@Severity": "text",
    "@Category": "text",
    "@Fact": "text",
    "@CurrentValue": "text",
    "@Threshold": "text",
    "@Operator": "text",
    "@WindowMin": "text",
    "@Timestamp": "text",
    "@Process": "text",
    "@GroupBy": "text",
    "@GroupValue": "text",
    "@AffectedCount": "text",
    "@AffectedEquipment": "text",
    "@GrafanaUrl": "url",
    "@Hostname": "text",
    "@Model": "text",
    "@Line": "text",
    "@IP": "text",
    "@CODE": "text",
}

ROW_TOKENS: dict[str, str] = {
    "@Row.Index": "text",
    "@Row.EqpId": "text",
    "@Row.CurrentValue": "text",
    "@Row.Threshold": "text",
    "@Row.Severity": "text",
    "@Row.Fact": "text",
    "@Row.Category": "text",
    "@Row.Operator": "text",
    "@Row.Model": "text",
    "@Row.Line": "text",
    "@Row.IP": "text",
    "@Row.Proc": "text",
}

# union view used by the renderer/lint (scalar + row token names are disjoint)
TOKEN_CONTEXT: dict[str, str] = {**SCALAR_TOKENS, **ROW_TOKENS}
