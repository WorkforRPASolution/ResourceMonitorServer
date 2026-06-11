"""Tests for src.alert.body_renderer — pure renderer for the RMS alert body.

Covers escape (D6), number/None/str formatting (decided: str verbatim), scalar
token substitution + prefix-collision safety, and unknown/missing token policy.
See docs/resource-monitor-email-template-architecture.md §7.2.
"""
import json
import pathlib

import pytest

from src.alert.body_renderer import (
    DEFAULT_BODY,
    DEFAULT_TITLE,
    order_rows,
    render_body,
    render_title,
)

_GOLDEN = json.loads(
    (pathlib.Path(__file__).parent.parent / "data" / "email_template_golden.json").read_text(encoding="utf-8")
)
_GOLDEN_CASES = _GOLDEN["cases"]

ERB = "<!--@EachEquipment-->"
END = "<!--@EndEachEquipment-->"


@pytest.mark.unit
class TestEscape:
    def test_text_token_html_escaped(self):
        out = render_body("[@Fact]", {"@Fact": 'a<b&c"\''})
        assert out == "[a&lt;b&amp;c&quot;&#x27;]"

    def test_url_token_keeps_query_amp(self):
        url = "https://h/d/uid?var-eqpId=E1&var-process=P"
        out = render_body('<a href="@GrafanaUrl">c</a>', {"@GrafanaUrl": url})
        assert out == f'<a href="{url}">c</a>'
        assert "&var-process=P" in out  # query '&' preserved
        assert "&amp;" not in out

    def test_url_token_escapes_quote(self):
        out = render_body('<a href="@GrafanaUrl">c</a>',
                          {"@GrafanaUrl": 'https://h/?x="y'})
        assert '"y' not in out
        assert "&quot;y" in out

    def test_url_token_rejects_non_http(self):
        out = render_body('<a href="@GrafanaUrl">c</a>',
                          {"@GrafanaUrl": "javascript:alert(1)"})
        assert "javascript" not in out
        assert out == '<a href="">c</a>'


@pytest.mark.unit
class TestFormatting:
    def test_none_renders_dash(self):
        assert render_body("[@CurrentValue]", {"@CurrentValue": None}) == "[-]"

    def test_renderer_receives_raw_value_not_stringified_none(self):
        # Renderer must get raw None (not the literal "None"); proves the
        # str()-before-render seam is gone (architecture §7.2-C).
        out = render_body("[@CurrentValue]", {"@CurrentValue": None})
        assert "None" not in out

    def test_number_str_verbatim(self):
        out = render_body("[@CurrentValue][@Threshold]",
                          {"@CurrentValue": 91.2, "@Threshold": 85.0})
        assert out == "[91.2][85.0]"

    def test_str_threshold_verbatim(self):
        # trend / string thresholds render verbatim (no float formatting)
        assert render_body("[@Threshold]", {"@Threshold": "up"}) == "[up]"


@pytest.mark.unit
class TestScalarSubstitution:
    def test_scalar_substitution_all(self):
        tpl = "S=@Severity C=@Category V=@CurrentValue T=@Threshold W=@WindowMin"
        out = render_body(tpl, {
            "@Severity": "CRITICAL", "@Category": "CPU",
            "@CurrentValue": 91.2, "@Threshold": 85.0, "@WindowMin": 30,
        })
        assert out == "S=CRITICAL C=CPU V=91.2 T=85.0 W=30"

    def test_no_prefix_collision(self):
        # @ThresholdX is not a known token → left literal; @Threshold substituted.
        out = render_body("[@Threshold][@ThresholdX]", {"@Threshold": "85"})
        assert out == "[85][@ThresholdX]"

    def test_unknown_token_left_literal(self):
        assert render_body("[@Foo]", {}) == "[@Foo]"

    def test_known_but_missing_value_blank(self):
        # known token not provided → blank (e.g. @AffectedCount in single mode)
        assert render_body("[@Severity]", {}) == "[]"

    def test_email_address_in_template_preserved(self):
        # a literal email address must not be mangled by token substitution
        out = render_body("contact user@example.com please", {})
        assert out == "contact user@example.com please"


@pytest.mark.unit
class TestEquipmentRepeatBlock:
    def test_erb_single_row(self):
        tpl = f"<table>{ERB}<tr><td>@Row.EqpId</td></tr>{END}</table>"
        out = render_body(tpl, {}, [{"@Row.EqpId": "EQP001"}])
        assert out == "<table><tr><td>EQP001</td></tr></table>"

    def test_erb_n_rows_in_given_order(self):
        tpl = f"<table>{ERB}<tr><td>@Row.EqpId</td><td>@Row.CurrentValue</td></tr>{END}</table>"
        rows = [
            {"@Row.EqpId": "EQP001", "@Row.CurrentValue": 91.2},
            {"@Row.EqpId": "EQP002", "@Row.CurrentValue": 88.9},
            {"@Row.EqpId": "EQP003", "@Row.CurrentValue": 86.1},
        ]
        out = render_body(tpl, {}, rows)
        assert out == (
            "<table>"
            "<tr><td>EQP001</td><td>91.2</td></tr>"
            "<tr><td>EQP002</td><td>88.9</td></tr>"
            "<tr><td>EQP003</td><td>86.1</td></tr>"
            "</table>"
        )

    def test_erb_row_index_is_one_based(self):
        tpl = f"{ERB}[@Row.Index]{END}"
        out = render_body(tpl, {}, [{"@Row.EqpId": "A"}, {"@Row.EqpId": "B"}])
        assert out == "[1][2]"

    def test_erb_row_none_value_dash(self):
        tpl = f"{ERB}[@Row.CurrentValue]{END}"
        out = render_body(tpl, {}, [{"@Row.CurrentValue": None}])
        assert out == "[-]"

    def test_scalar_inside_block_substituted_each_row(self):
        # scalar tokens inside the block survive row expansion and are filled
        # once-per-row in the scalar pass; row tokens outside the block blank.
        tpl = f"@Severity|{ERB}[@Row.EqpId@Severity]{END}|@Row.EqpId"
        out = render_body(tpl, {"@Severity": "CRIT"}, [{"@Row.EqpId": "E1"}])
        assert out == "CRIT|[E1CRIT]|"

    def test_no_block_rows_ignored(self):
        out = render_body("@Severity", {"@Severity": "X"}, [{"@Row.EqpId": "E"}])
        assert out == "X"

    def test_order_rows_severity_then_value_then_eqpid(self):
        rows = [
            {"@Row.EqpId": "E2", "@Row.Severity": "WARNING", "@Row.CurrentValue": 88.0},
            {"@Row.EqpId": "E1", "@Row.Severity": "CRITICAL", "@Row.CurrentValue": 91.0},
            {"@Row.EqpId": "E3", "@Row.Severity": "CRITICAL", "@Row.CurrentValue": 95.0},
            {"@Row.EqpId": "E0", "@Row.Severity": "WARNING", "@Row.CurrentValue": None},
        ]
        ordered = [r["@Row.EqpId"] for r in order_rows(rows)]
        # CRITICAL desc-by-value (E3=95, E1=91) → WARNING (E2=88, then None last E0)
        assert ordered == ["E3", "E1", "E2", "E0"]


@pytest.mark.unit
class TestMalformedErbIsTotal:
    """The canonical renderer must be *total* on malformed ERB fences (gap found
    by the 2026-06 completeness audit). RMS reads templates straight from Mongo,
    so a seed/migration/manual-edit that bypasses the WebManager ERB lint must
    not crash the alert pipeline nor leak markers/blank rows into the email."""

    def test_start_without_end_does_not_raise_and_strips_marker(self):
        # previously: rest.split(ERB_END, 1) raised ValueError (unpack) → caller
        # swallowed it into DEFAULT_BODY. Now total: no marker leak, no crash.
        tpl = f"<table>{ERB}<tr><td>@Row.EqpId</td></tr></table>"  # no END
        out = render_body(tpl, {}, [{"@Row.EqpId": "EQP001"}])
        assert ERB not in out and END not in out
        assert "@EachEquipment" not in out
        assert "<table>" in out and "</table>" in out

    def test_end_only_strips_marker(self):
        out = render_body(f"<p>x</p>{END}", {}, [])
        assert ERB not in out and END not in out
        assert "@EachEquipment" not in out and "@EndEachEquipment" not in out
        assert "<p>x</p>" in out

    def test_leading_stray_end_then_valid_block_still_expands(self):
        tpl = f"{END}<table>{ERB}<tr>@Row.EqpId</tr>{END}</table>"
        out = render_body(tpl, {}, [{"@Row.EqpId": "EQP001"}])
        assert ERB not in out and END not in out
        assert "EQP001" in out  # the first balanced block still expands

    def test_duplicate_blocks_drop_second_no_marker_no_blank_row(self):
        tpl = (
            f"<table>{ERB}<tr>R1=@Row.EqpId</tr>{END}"
            f"MID{ERB}<tr>R2=@Row.EqpId</tr>{END}</table>"
        )
        out = render_body(tpl, {}, [{"@Row.EqpId": "EQP001"}])
        assert ERB not in out and END not in out      # no marker leak (issue #2)
        assert "R1=EQP001" in out                       # first block expanded
        assert "R2" not in out                          # second block dropped whole
        assert "<tr></tr>" not in out                   # no blank row


@pytest.mark.unit
class TestSizeGuards:
    def test_erb_row_cap_with_overflow(self):
        tpl = f"{ERB}<tr><td>@Row.EqpId</td></tr>{END}"
        rows = [{"@Row.EqpId": f"E{i}"} for i in range(5)]
        out = render_body(
            tpl, {}, rows, row_limit=3,
            overflow_text="<tr><td>외 @RemainingCount대</td></tr>",
        )
        assert out.count("<tr>") == 4  # 3 rows + overflow row
        assert "E0" in out and "E2" in out and "E3" not in out
        assert "외 2대" in out

    def test_no_overflow_when_within_limit(self):
        tpl = f"{ERB}<tr><td>@Row.EqpId</td></tr>{END}"
        rows = [{"@Row.EqpId": "E0"}, {"@Row.EqpId": "E1"}]
        out = render_body(tpl, {}, rows, row_limit=3,
                          overflow_text="<tr><td>외 @RemainingCount대</td></tr>")
        assert "외" not in out
        assert out.count("<tr>") == 2

    def test_byte_cap_truncates_under_limit(self):
        big = "x" * 1000
        out = render_body(big, {}, byte_cap=200)
        assert len(out.encode("utf-8")) <= 200
        assert out != big

    def test_byte_cap_noop_when_within(self):
        out = render_body("small", {}, byte_cap=200)
        assert out == "small"


@pytest.mark.unit
class TestTitleRender:
    def test_title_strips_colon(self):
        # EmailActor splits "title:body" on the first ':' → title must be colon-free
        out = render_title("[EARS] @Category: @Severity",
                           {"@Category": "CPU", "@Severity": "CRITICAL"})
        assert ":" not in out
        assert "CPU" in out and "CRITICAL" in out

    def test_title_empty_uses_default(self):
        assert render_title("", {}) == DEFAULT_TITLE
        assert ":" not in DEFAULT_TITLE  # default must also be colon-safe

    def test_title_substitutes_plain_not_html_escaped(self):
        # subject is plain text, not HTML → no entity escaping
        out = render_title("@Fact", {"@Fact": "a<b&c"})
        assert out == "a<b&c"
        assert "&amp;" not in out


@pytest.mark.unit
class TestReservedTokenNeutralize:
    def test_data_value_neutralizes_reserved_token(self):
        # Akka substitutes @HttpWebServerAddress in renderedBody (D2); a literal
        # occurrence inside a *data value* must be neutralized so Akka can't mangle it.
        out = render_body("[@Fact]", {"@Fact": "see @HttpWebServerAddress now"})
        assert "@HttpWebServerAddress" not in out
        assert "&#64;HttpWebServerAddress" in out

    def test_template_reserved_token_preserved(self):
        # operator-inserted image URL placeholder in the TEMPLATE stays for Akka
        out = render_body("img http://@HttpWebServerAddress/x.png", {})
        assert "@HttpWebServerAddress" in out


@pytest.mark.unit
class TestDefaultBody:
    def test_builtin_default_body_renders(self):
        scalars = {
            "@Category": "CPU", "@Severity": "CRITICAL", "@Process": "PHOTO",
            "@WindowMin": "30", "@Timestamp": "2026-06-09 14:05 KST",
            "@AffectedCount": "1",
        }
        rows = [{
            "@Row.EqpId": "EQP001", "@Row.Fact": "cpu.total",
            "@Row.CurrentValue": "91.2", "@Row.Threshold": "85.0",
            "@Row.Severity": "CRITICAL",
        }]
        out = render_body(DEFAULT_BODY, scalars, rows)
        # all provided tokens substituted (no leftover @-tokens), values present
        assert "@Category" not in out and "@Severity" not in out
        assert "@Row." not in out
        assert "EQP001" in out and "91.2" in out and "PHOTO" in out


@pytest.mark.unit
@pytest.mark.parametrize("case", _GOLDEN_CASES, ids=[c["name"] for c in _GOLDEN_CASES])
def test_golden_case_matches(case):
    body = render_body(case["template_html"], case["scalars"], case["rows"])
    title = render_title(case["title_template"], case["scalars"])
    assert body == case["expected_body"], case["name"]
    assert title == case["expected_title"], case["name"]


@pytest.mark.unit
def test_golden_is_nonempty_and_covers_modes():
    names = {c["name"] for c in _GOLDEN_CASES}
    assert {"single_cpu_critical", "group_three_rows", "escape_in_value"} <= names


@pytest.mark.unit
def test_golden_meta_self_test():
    """The runner must detect a flipped expected (not a fake-green)."""
    case = _GOLDEN_CASES[0]
    body = render_body(case["template_html"], case["scalars"], case["rows"])
    assert body == case["expected_body"]
    assert body != case["expected_body"] + "X"
