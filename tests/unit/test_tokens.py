"""Tests for src.alert.tokens — token catalog for the RMS email body renderer.

The catalog drives the renderer's per-token escape context and is the single
source the WebManager palette/lint mirror. See
docs/resource-monitor-email-template-architecture.md §7.2.
"""
import pytest

from src.alert.tokens import ROW_TOKENS, SCALAR_TOKENS, TOKEN_CONTEXT


@pytest.mark.unit
class TestTokenCatalog:
    def test_grafana_url_is_url_context(self):
        assert SCALAR_TOKENS["@GrafanaUrl"] == "url"

    def test_all_non_url_scalars_are_text(self):
        for tok, ctx in SCALAR_TOKENS.items():
            if tok == "@GrafanaUrl":
                continue
            assert ctx == "text", f"{tok} should be text context, got {ctx}"

    def test_metric_excluded_fact_included(self):
        # @Metric is v1-excluded (no data source); @Fact is the v1 metric token.
        assert "@Metric" not in SCALAR_TOKENS
        assert "@Fact" in SCALAR_TOKENS

    def test_core_scalar_tokens_present(self):
        for tok in (
            "@Severity", "@Category", "@CurrentValue", "@Threshold",
            "@Operator", "@WindowMin", "@Timestamp", "@Process",
            "@GroupBy", "@GroupValue", "@AffectedCount", "@AffectedEquipment",
            "@Hostname", "@Model", "@Line", "@IP", "@CODE",
        ):
            assert tok in SCALAR_TOKENS, f"missing scalar token {tok}"

    def test_row_tokens_namespaced_and_text(self):
        assert ROW_TOKENS, "row tokens must be non-empty"
        for tok, ctx in ROW_TOKENS.items():
            assert tok.startswith("@Row."), f"{tok} must be @Row.* namespaced"
            assert ctx == "text", f"{tok} should be text context"
        for tok in (
            "@Row.Index", "@Row.EqpId", "@Row.CurrentValue",
            "@Row.Threshold", "@Row.Severity",
        ):
            assert tok in ROW_TOKENS, f"missing row token {tok}"

    def test_context_is_disjoint_union(self):
        # No name overlap between scalar and row tokens; TOKEN_CONTEXT is the union.
        assert set(SCALAR_TOKENS) & set(ROW_TOKENS) == set()
        assert TOKEN_CONTEXT == {**SCALAR_TOKENS, **ROW_TOKENS}
