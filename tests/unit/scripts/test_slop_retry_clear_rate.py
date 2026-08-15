"""Pins the clear-rate reading behind issue #1530.

The formula is the whole product of this script, and #1434's adversarial review named the one way
to get it wrong: the newsletter loop shares its single regeneration with the structural floor
(#1435), so an `unsteered` row spent a call with no HARD check to fix. Counting those as cleared —
or even as denominator — inflates exactly the number the budget decision turns on.
"""

import importlib.util
import pathlib

import pytest

pytestmark = pytest.mark.unit

_SCRIPT = pathlib.Path("scripts/slop_retry_clear_rate.py")


@pytest.fixture(scope="module")
def tool():
    spec = importlib.util.spec_from_file_location("slop_retry_clear_rate", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _row(outcome: str, surface: str = "newsletter", before=("contrastive_frame",),
         after=(), kept: bool = True, hard_before=None):
    """One `slop_retry` row in the shape PostHog hands back (labels as STRINGS)."""
    return {
        "surface": surface,
        "outcome": outcome,
        "kept": "True" if kept else "False",
        "hard_before": len(before) if hard_before is None else hard_before,
        "hard_after": len(after),
        "attempt": "2",
        "max_attempts": "2",
        "before_checks": list(before),
        "after_checks": list(after),
        "timestamp": "2026-09-01T10:00:00Z",
    }


class TestParseRows:
    def test_a_result_tuple_reads_the_same_as_a_dict(self, tool):
        # PostHog returns positional tuples for a HogQL query; the report must not care.
        tuple_row = ("newsletter", "cleared", "True", 1, 0, 2, 2, ["banned_lexicon"], [],
                     "2026-09-01T10:00:00Z")
        assert tool.parse_rows([tuple_row]) == tool.parse_rows([_row("cleared",
                                                                    before=("banned_lexicon",))])

    def test_a_label_boolean_arrives_as_a_string_and_is_still_a_boolean(self, tool):
        # `kept` rides as a label(), so PostHog stores "True" — read as text it would always be
        # truthy and the kept share would be 100% on every window.
        assert tool.parse_rows([_row("persisted", kept=False)])[0]["kept"] is False

    def test_check_names_survive_arriving_as_json_text(self, tool):
        row = _row("traded")
        row["before_checks"] = '["contrastive_frame", "banned_lexicon"]'
        assert tool.parse_rows([row])[0]["before_checks"] == ["contrastive_frame",
                                                             "banned_lexicon"]

    def test_an_unreadable_count_is_none_rather_than_zero(self, tool):
        row = _row("cleared")
        row["hard_before"] = "not a number"
        assert tool.parse_rows([row])[0]["hard_before"] is None


class TestMeasure:
    def test_unsteered_rows_are_excluded_from_the_clear_rate(self, tool):
        # THE guard from #1434's review: 1 cleared of 2 steered is 50%, not 33% and not 66% —
        # the unsteered row is neither a success nor a failure of the slop retry.
        rows = tool.parse_rows([_row("cleared"), _row("persisted"),
                                _row("unsteered", before=(), after=())])
        result = tool.measure(rows)
        assert (result["rows"], result["steered"]) == (3, 2)
        assert result["clear_rate"] == 50.0

    def test_the_unsteered_share_is_stated_against_every_row(self, tool):
        # It is how much of the budget the OTHER grader spends — an argument about whether the two
        # should share one attempt at all, so it is not a share of the steered rows.
        result = tool.measure(tool.parse_rows([_row("cleared"), _row("unsteered", before=())]))
        assert (result["unsteered"], result["unsteered_share"]) == (1, 50.0)

    def test_traded_is_reported_apart_from_persisted(self, tool):
        # The two point at different fixes: traded says no attempt budget helps.
        rows = tool.parse_rows([_row("traded", before=("contrastive_frame",),
                                     after=("banned_lexicon",)),
                                _row("persisted"), _row("persisted"), _row("cleared")])
        result = tool.measure(rows)
        assert (result["traded_share"], result["persisted_share"]) == (25.0, 50.0)

    def test_lost_rows_stay_in_the_denominator(self, tool):
        # An empty regeneration still spent the call; dropping it would flatter the clear-rate.
        result = tool.measure(tool.parse_rows([_row("cleared"), _row("lost", after=())]))
        assert (result["steered"], result["clear_rate"]) == (2, 50.0)

    def test_an_empty_window_reports_none_rather_than_a_measured_zero(self, tool):
        result = tool.measure([])
        assert (result["rows"], result["steered"]) == (0, 0)
        assert result["clear_rate"] is None and result["unsteered_share"] is None

    def test_a_graded_row_with_no_hard_check_going_in_is_counted_and_named(self, tool):
        # Only `lost` can land here (no `after` report to grade), and it is the one row where the
        # outcome denominator and a `hard_before > 0` denominator disagree.
        result = tool.measure(tool.parse_rows([_row("lost", before=(), hard_before=0),
                                               _row("cleared")]))
        assert result["graded_without_hard_before"] == 1

    def test_an_unreadable_hard_count_is_not_read_as_a_clean_draft(self, tool):
        # `_as_int` returns None for an unreadable count on purpose; treating that None as 0 would
        # report the row as having carried no HARD check going in, which is a claim about the draft
        # that the data does not support.
        row = _row("persisted")
        row["hard_before"] = "not a number"
        assert tool.measure(tool.parse_rows([row]))["graded_without_hard_before"] == 0

    def test_an_outcome_this_reader_does_not_know_is_reported_not_dropped(self, tool):
        # A sixth verdict added to retry_outcome — or an outcome that failed to ingest — must not
        # vanish: leaving it out of both denominators shrinks the one the clear-rate is read off,
        # i.e. RAISES the number, with nothing on screen saying the sample was cut.
        rows = tool.parse_rows([_row("cleared"), _row("persisted"), _row("rewritten")])
        result = tool.measure(rows)
        assert (result["rows"], result["steered"]) == (3, 2)
        assert result["unrecognised"] == {"rewritten": 1}
        assert "UNRECOGNISED OUTCOMES" in tool.format_report(result, days=30)

    def test_an_unknown_outcome_never_counts_toward_the_kept_share(self, tool):
        # kept was tallied for every row that was not `unsteered`, so an unknown outcome could put
        # the numerator above the steered denominator and print a kept share over 100%.
        rows = tool.parse_rows([_row("cleared", kept=True), _row("rewritten", kept=True)])
        assert tool.measure(rows)["kept_share"] == 100.0

    def test_the_kept_share_counts_only_steered_rows(self, tool):
        rows = tool.parse_rows([_row("cleared", kept=True), _row("persisted", kept=False)])
        assert tool.measure(rows)["kept_share"] == 50.0

    def test_each_surface_is_graded_on_its_own_rows(self, tool):
        # A budget is per surface (SLOP_LINT_MAX_ATTEMPTS_<SURFACE>), so a pooled rate decides
        # nothing — a comment loop at volume would swamp a weekly edition.
        rows = tool.parse_rows([_row("cleared", surface="newsletter"),
                                _row("persisted", surface="comment"),
                                _row("cleared", surface="comment")])
        result = tool.measure(rows)
        assert result["by_surface"]["newsletter"]["clear_rate"] == 100.0
        assert result["by_surface"]["comment"]["clear_rate"] == 50.0

    def test_a_row_counts_under_every_check_it_was_steered_on(self, tool):
        rows = tool.parse_rows([_row("cleared", before=("contrastive_frame", "banned_lexicon")),
                                _row("persisted", before=("contrastive_frame",))])
        result = tool.measure(rows)
        assert result["by_check"]["contrastive_frame"]["steered"] == 2
        assert result["by_check"]["banned_lexicon"]["clear_rate"] == 100.0


class TestQuery:
    def test_the_query_reads_the_slop_retry_event_over_the_window(self, tool):
        sql = tool.build_query(days=30)
        assert "slop_retry" in sql and "INTERVAL 30 DAY" in sql

    def test_a_surface_filter_narrows_the_query(self, tool):
        assert "properties.surface = 'newsletter'" in tool.build_query(surface="newsletter")

    def test_a_surface_that_is_not_an_identifier_is_refused(self, tool):
        # The one argument that reaches HogQL as text. A quote does not belong in a surface name,
        # and interpolating one rewrites the predicate rather than filtering on it.
        with pytest.raises(ValueError):
            tool.build_query(surface="newsletter' OR '1'='1")


class TestReport:
    def test_a_thin_sample_refuses_to_state_a_rate(self, tool):
        # A two-row window renders a perfectly well-formed percentage that reads as measured.
        text = tool.format_report(tool.measure(tool.parse_rows([_row("cleared"),
                                                               _row("persisted")])), days=30)
        assert "NOT ENOUGH DATA" in text and "%" not in text

    def test_a_breakdown_row_under_the_floor_prints_counts_not_a_rate(self, tool):
        # The per-check split divides the sample again — this is where a "100.0%" read off one
        # regeneration would otherwise appear under an overall rate that IS earned.
        rows = tool.parse_rows([_row("cleared") for _ in range(10)]
                               + [_row("cleared", before=("banned_lexicon",))])
        text = tool.format_report(tool.measure(rows), days=30)
        assert "clear-rate 100.0%" in text
        assert "banned_lexicon         steered    1  cleared 1" in text

    def test_a_window_that_filled_the_row_cap_says_it_was_truncated(self, tool):
        # The header states a window; a query that returned its cap read only the newest slice of
        # it, and the rate is off that slice.
        rows = tool.parse_rows([_row("cleared") for _ in range(12)])
        text = tool.format_report(tool.measure(rows), days=30, limit=12)
        assert "TRUNCATED" in text
        assert "TRUNCATED" not in tool.format_report(tool.measure(rows), days=30, limit=10000)

    def test_the_sample_and_the_window_lead_the_report(self, tool):
        rows = tool.parse_rows([_row("cleared") for _ in range(10)])
        text = tool.format_report(tool.measure(rows), days=30)
        assert text.startswith("slop_retry — 10 rows over the last 30 days")
        assert "clear-rate 100.0%" in text


class TestMain:
    def test_print_sql_needs_no_key_and_touches_no_network(self, tool, monkeypatch, capsys):
        monkeypatch.delenv("POSTHOG_PERSONAL_API_KEY", raising=False)
        assert tool.main(["--print-sql"]) == 0
        assert "FROM events" in capsys.readouterr().out

    def test_a_missing_key_fails_rather_than_printing_an_empty_reading(self, tool, monkeypatch,
                                                                      capsys):
        monkeypatch.delenv("POSTHOG_PERSONAL_API_KEY", raising=False)
        assert tool.main([]) == 1
        assert "nothing was measured" in capsys.readouterr().out

    def test_a_bad_surface_exits_one_rather_than_raising(self, tool, monkeypatch, capsys):
        monkeypatch.delenv("POSTHOG_PERSONAL_API_KEY", raising=False)
        assert tool.main(["--print-sql", "--surface", "news letter"]) == 1
        assert "bare identifier" in capsys.readouterr().out

    def test_a_thin_window_exits_non_zero(self, tool, monkeypatch, capsys):
        monkeypatch.setenv("POSTHOG_PERSONAL_API_KEY", "phx_test")
        monkeypatch.setattr(tool.PostHogQueryClient, "query",
                            lambda self, hogql: [_row("cleared")])
        assert tool.main([]) == 1
        assert "NOT ENOUGH DATA" in capsys.readouterr().out

    def test_a_full_window_prints_the_rate_and_exits_zero(self, tool, monkeypatch, capsys):
        monkeypatch.setenv("POSTHOG_PERSONAL_API_KEY", "phx_test")
        monkeypatch.setattr(tool.PostHogQueryClient, "query",
                            lambda self, hogql: [_row("cleared") for _ in range(12)])
        assert tool.main([]) == 0
        assert "clear-rate 100.0%" in capsys.readouterr().out
