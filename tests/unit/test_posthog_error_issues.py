"""Unit tests for scripts/posthog_error_issues.py — the PostHog-issue -> GitHub-issue filer that
replaces the log-grep cron (issue #648).
"""

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
import requests

pytestmark = pytest.mark.unit

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "posthog_error_issues.py"


@pytest.fixture(scope="module")
def mod():
    spec = importlib.util.spec_from_file_location("posthog_error_issues", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["posthog_error_issues"] = module
    spec.loader.exec_module(module)
    return module


def _row(mod, issue_id="11111111-2222-3333-4444-555555555555", **overrides):
    row = {"issue_id": issue_id, "name": "RuntimeError", "description": "selenium session died",
           "status": "active", "first_seen": "2026-07-25T00:00:00Z",
           "last_seen": "2026-07-26T00:00:00Z", "occurrences": 12, "users": 2,
           "lib": "posthog-python", "task_name": "automate_commenting", "route": None}
    row.update(overrides)
    return row


class TestBuildQuery:
    def test_selects_the_error_tracking_columns_and_window(self, mod):
        sql = mod.build_query(hours=6, min_occurrences=3)
        assert "event = '$exception'" in sql
        assert "INTERVAL 6 HOUR" in sql
        assert "GROUP BY issue_id" in sql
        assert "HAVING occurrences >= 3" in sql

    def test_clamps_nonsense_inputs(self, mod):
        sql = mod.build_query(hours=0, min_occurrences=0)
        assert "INTERVAL 24 HOUR" in sql
        assert "HAVING occurrences >= 1" in sql


class TestParseRows:
    def test_zips_columns_onto_rows(self, mod):
        rows = mod.parse_rows([["id-1", "RuntimeError", "boom", "active", "t0", "t1", 3, 1,
                               "posthog-python", "automate_commenting", None]])
        assert rows[0]["issue_id"] == "id-1"
        assert rows[0]["name"] == "RuntimeError"
        assert rows[0]["occurrences"] == 3

    def test_drops_rows_without_an_issue_id(self, mod):
        assert mod.parse_rows([["", "RuntimeError"], [None, "x"]]) == []

    def test_tolerates_short_rows_and_garbage(self, mod):
        rows = mod.parse_rows([["id-1", "RuntimeError"], "not-a-row", None])
        assert len(rows) == 1
        assert rows[0]["occurrences"] is None

    def test_handles_no_results(self, mod):
        assert mod.parse_rows(None) == []


class TestActionability:
    @pytest.mark.parametrize("status", ["active", "ACTIVE", "", None])
    def test_active_issues_are_filed(self, mod, status):
        assert mod.is_actionable(_row(mod, status=status)) is True

    @pytest.mark.parametrize("status", ["resolved", "suppressed", "archived", "pending_release"])
    def test_triaged_issues_are_left_alone(self, mod, status):
        assert mod.is_actionable(_row(mod, status=status)) is False


class TestTitleAndBody:
    def test_title_is_conventional_commit_shaped_with_the_count(self, mod):
        title = mod.build_title(_row(mod))
        assert title.startswith("fix(errors): RuntimeError: selenium session died")
        assert title.endswith("(12x)")

    def test_title_is_truncated_but_keeps_the_count(self, mod):
        title = mod.build_title(_row(mod, description="x" * 400))
        assert len(title) <= mod.MAX_TITLE_CHARS
        assert title.endswith("(12x)")

    def test_title_survives_a_nameless_issue(self, mod):
        assert "Unknown exception" in mod.build_title(_row(mod, name=None, description=None))

    def test_body_carries_the_marker_context_and_posthog_link(self, mod):
        row = _row(mod)
        body = mod.build_body(row, hours=24, project_id="475262")
        assert mod.marker(row["issue_id"]) in body
        assert "automate_commenting" in body
        assert "error_tracking/" + row["issue_id"] in body
        assert "## Acceptance" in body

    def test_body_omits_context_it_does_not_have(self, mod):
        body = mod.build_body(_row(mod, task_name=None, route=None, lib=None))
        assert "Celery task:" not in body
        assert "API route:" not in body


class TestBuildComment:
    def _comment(self, mod, **overrides):
        row = _row(mod, name="RecurringWarning",
                   description="Selector miss: Comment sort control", **overrides)
        return mod.build_comment(row, {"number": 818,
                                       "signature": "Selector miss: Comment sort control"},
                                 hours=24, project_id="475262")

    def test_carries_the_marker_so_the_next_run_skips_the_row(self, mod):
        body = self._comment(mod)
        assert mod.marker("11111111-2222-3333-4444-555555555555") in body

    def test_carries_the_fresh_occurrence_data_and_the_posthog_link(self, mod):
        body = self._comment(mod)
        assert "Occurrences (last 24h): **12**" in body
        assert "automate_commenting" in body
        assert "error_tracking/11111111-2222-3333-4444-555555555555" in body

    def test_says_what_it_matched_on_so_a_bad_merge_is_visible(self, mod):
        assert "`Selector miss: Comment sort control`" in self._comment(mod)

    def test_it_is_not_a_mode_start_issue_body(self, mod):
        body = self._comment(mod)
        assert "## Acceptance" not in body
        assert "## Scope" not in body

    def test_a_browser_exception_still_links_its_replay(self, mod):
        body = self._comment(mod, lib="web", session_id="0198f0aa-1b2c-7000-8000-abcdef012345")
        assert "/replay/0198f0aa-1b2c-7000-8000-abcdef012345" in body

    def test_survives_a_match_with_no_recorded_signature(self, mod):
        row = _row(mod, name="RecurringWarning", description="Selector miss: Comment sort control")
        assert "no duplicate" in mod.build_comment(row, {"number": 818})

    def test_it_never_offers_deletion_as_the_way_to_split_a_wrong_match(self, mod):
        # Deleting the comment does NOT get the warning its own issue: the matched issue still
        # carries the string, so the next run matches it again and re-posts here forever
        # (pinned by TestOpenMatches.test_deleting_the_comment_re_matches_the_same_tracker).
        body = self._comment(mod)
        assert "delete this comment" not in body.casefold()
        assert "open a separate issue" in body.casefold()


class TestReplayLink:
    _SESSION = "0198f0aa-1b2c-7000-8000-abcdef012345"

    def test_query_pulls_a_session_id_for_the_replay(self, mod):
        assert "any(properties.$session_id) AS session_id" in mod.build_query()
        assert mod.COLUMNS[-1] == "session_id"

    def test_parse_rows_reads_the_session_id_column(self, mod):
        rows = mod.parse_rows([["id-1", "TypeError", "boom", "active", "t0", "t1", 2, 1,
                                "web", None, "/content", self._SESSION]])
        assert rows[0]["session_id"] == self._SESSION

    def test_a_browser_exception_links_its_replay(self, mod):
        body = mod.build_body(_row(mod, lib="web", session_id=self._SESSION),
                              project_id="475262")
        assert f"/project/475262/replay/{self._SESSION}" in body
        assert body.index("replay/") < body.index("## Scope")

    def test_a_backend_exception_has_no_replay_line(self, mod):
        assert "session replay" not in mod.build_body(_row(mod))

    @pytest.mark.parametrize("session_id", [None, "", "  ", "short", "has space",
                                            ")](https://evil.example"])
    def test_an_unshaped_session_id_is_never_linked(self, mod, session_id):
        assert mod.replay_url(session_id) is None
        assert "replay/" not in mod.build_body(_row(mod, session_id=session_id))


class TestWarningSignature:
    """#1083 — the normalized string an escalated warning is recognised by in an existing tracker."""

    def test_a_recurring_warning_carries_its_normalized_message(self, mod):
        row = _row(mod, name="RecurringWarning", description="Selector miss: Comment sort control")
        assert mod.warning_signature(row) == "Selector miss: Comment sort control"

    def test_a_raw_exception_has_no_signature(self, mod):
        # Its description is the interpolated message, not a masked template — matching it against
        # a human's prose would merge on coincidence.
        assert mod.warning_signature(_row(mod, name="RuntimeError")) is None

    @pytest.mark.parametrize("description", [None, "", "boom", "too short", "a b c"])
    def test_a_vague_message_is_refused_rather_than_matched_loosely(self, mod, description):
        assert mod.warning_signature(
            _row(mod, name="RecurringWarning", description=description)) is None

    def test_whitespace_is_collapsed(self, mod):
        row = _row(mod, name="RecurringWarning", description="  Selector miss:   Feed  sort  ")
        assert mod.warning_signature(row) == "Selector miss: Feed sort"


class TestIssueMatching:
    _SIG = "Selector miss: Comment sort control"

    def test_matches_a_hand_filed_title(self, mod):
        # The real #818: it quotes the warning in its title, and #1063 was auto-filed anyway.
        issue = {"number": 818,
                 "title": "fix(observability): 'Selector miss: Comment sort control' — comment-"
                          "demotion denominator is silently shrinking",
                 "body": "## Why\n..."}
        assert mod.issue_matches(self._SIG, issue) is True

    def test_matches_a_body_mention(self, mod):
        issue = {"number": 816, "title": "reaction flow broken by SDUI drift",
                 "body": "| `Selector miss: Comment sort control` | 30 | 3 |"}
        assert mod.issue_matches(self._SIG, issue) is True

    def test_casing_differences_still_match(self, mod):
        assert mod.issue_matches(self._SIG, {"number": 1, "title": "selector MISS: comment sort "
                                                                   "control", "body": ""}) is True

    def test_a_different_warning_does_not_match(self, mod):
        assert mod.issue_matches(self._SIG, {"number": 1, "title": "Selector miss: Reaction state",
                                             "body": "nothing else"}) is False

    def test_a_tokenized_search_hit_is_only_a_candidate(self, mod):
        # gh's search matches on words; pick_match is where the literal phrase is enforced.
        hits = [{"number": 900, "title": "sort control comment selector", "body": "unrelated"}]
        assert mod.pick_match(self._SIG, hits) is None

    def test_picks_the_lowest_numbered_tracker(self, mod):
        hits = [{"number": 1063, "title": f"fix(errors): RecurringWarning: {self._SIG} (1x)",
                 "body": ""},
                {"number": 818, "title": f"'{self._SIG}' — denominator shrinking", "body": ""}]
        assert mod.pick_match(self._SIG, hits)["number"] == 818

    def test_a_hit_without_a_number_is_ignored(self, mod):
        assert mod.pick_match(self._SIG, [{"title": self._SIG, "body": ""}]) is None

    def test_no_hits_is_no_match(self, mod):
        assert mod.pick_match(self._SIG, None) is None

    def test_search_phrase_drops_quotes_that_would_close_it_early(self, mod):
        assert '"' not in mod.search_phrase('Selector miss: "Sort by" control')

    def test_search_phrase_is_truncated_but_the_full_string_still_decides(self, mod):
        long_signature = "Selector miss: " + ("x" * 400)
        assert len(mod.search_phrase(long_signature)) <= mod.MAX_SEARCH_CHARS
        assert mod.pick_match(long_signature,
                              [{"number": 5, "title": "Selector miss: xxx", "body": ""}]) is None

    def test_truncation_lands_on_a_word_boundary(self, mod):
        # GitHub tokenizes, so a phrase cut mid-word matches NOTHING — truncating must widen the
        # candidate set, never empty it.
        signature = ("Feed post-text walk matched nothing while the page still renders cards "
                     "selector drift observed repeatedly across the whole sweep window")
        phrase = mod.search_phrase(signature)
        assert len(phrase) <= mod.MAX_SEARCH_CHARS
        assert signature.startswith(phrase + " ")


class TestOpenMatches:
    def _gh(self, hits):
        gh = MagicMock()
        gh.search_open.return_value = hits
        return gh

    def test_a_hand_filed_tracker_is_matched(self, mod):
        row = _row(mod, issue_id="a", name="RecurringWarning",
                   description="Selector miss: Comment sort control")
        gh = self._gh([{"number": 818, "title": "'Selector miss: Comment sort control' — x",
                        "body": ""}])
        matches = mod.open_matches(gh, [row], already=set())
        assert matches[mod.marker("a")]["number"] == 818
        assert matches[mod.marker("a")]["signature"] == "Selector miss: Comment sort control"

    def test_a_prior_auto_filed_issue_is_matched(self, mod):
        # Same warning, NEW PostHog issue id (the fingerprint moved) — the open ticket it already
        # has is the right thread, so it must not get a second one.
        row = _row(mod, issue_id="new-id", name="RecurringWarning",
                   description="Selector miss: Reaction state")
        gh = self._gh([{"number": 874,
                        "title": "fix(errors): RecurringWarning: Selector miss: Reaction state (1x)",
                        "body": "Auto-filed. Dedup marker: `posthog-issue-old-id`"}])
        assert mod.open_matches(gh, [row], already=set())[mod.marker("new-id")]["number"] == 874

    def test_an_already_filed_row_is_not_searched(self, mod):
        row = _row(mod, issue_id="a", name="RecurringWarning",
                   description="Selector miss: Comment sort control")
        gh = self._gh([])
        assert mod.open_matches(gh, [row], already={mod.marker("a")}) == {}
        gh.search_open.assert_not_called()

    def test_a_resolved_row_is_not_searched(self, mod):
        row = _row(mod, issue_id="a", name="RecurringWarning", status="resolved",
                   description="Selector miss: Comment sort control")
        gh = self._gh([])
        assert mod.open_matches(gh, [row], already=set()) == {}
        gh.search_open.assert_not_called()

    def test_a_raw_exception_is_not_searched(self, mod):
        gh = self._gh([])
        assert mod.open_matches(gh, [_row(mod, issue_id="a")], already=set()) == {}
        gh.search_open.assert_not_called()

    def test_deleting_the_comment_re_matches_the_same_tracker(self, mod):
        # The comment is the ONLY durable record this run happened, and the text match outlives it:
        # with the marker gone, `is_filed` is False again and the tracker still carries the string.
        # So a deleted comment comes BACK — it never gets the warning an issue of its own, which is
        # why the comment tells the reader to open a separate issue instead.
        row = _row(mod, issue_id="a", name="RecurringWarning",
                   description="Selector miss: Comment sort control")
        gh = self._gh([{"number": 818, "title": "'Selector miss: Comment sort control' — x",
                        "body": ""}])
        assert mod.open_matches(gh, [row], already=set())[mod.marker("a")]["number"] == 818

    def test_no_match_leaves_the_row_to_be_filed(self, mod):
        row = _row(mod, issue_id="a", name="RecurringWarning",
                   description="Selector miss: Comment sort control")
        gh = self._gh([{"number": 7, "title": "something else", "body": "unrelated"}])
        assert mod.open_matches(gh, [row], already=set()) == {}


class TestPlanActions:
    def test_files_one_issue_per_unfiled_active_issue(self, mod):
        rows = [_row(mod, issue_id="a"), _row(mod, issue_id="b")]
        actions = mod.plan_actions(rows, filed_markers=set())
        assert [a["action"] for a in actions] == ["create", "create"]

    def test_an_already_filed_issue_is_never_filed_twice(self, mod):
        rows = [_row(mod, issue_id="a")]
        actions = mod.plan_actions(rows, filed_markers={mod.marker("a")})
        assert actions[0]["action"] == "skip"
        assert actions[0]["reason"] == "already filed"
        assert mod.pending(actions) == []

    def test_resolved_issues_are_skipped(self, mod):
        actions = mod.plan_actions([_row(mod, issue_id="a", status="resolved")], set())
        assert actions[0]["action"] == "skip"
        assert actions[0]["reason"] == "not active"

    def test_max_new_defers_the_rest(self, mod):
        rows = [_row(mod, issue_id=str(i)) for i in range(5)]
        actions = mod.plan_actions(rows, set(), max_new=2)
        assert [a["action"] for a in actions] == ["create", "create", "deferred", "deferred",
                                                  "deferred"]

    def test_duplicate_ids_in_one_batch_file_once(self, mod):
        rows = [_row(mod, issue_id="a"), _row(mod, issue_id="a")]
        assert len(mod.pending(mod.plan_actions(rows, set()))) == 1

    def test_a_matched_warning_comments_instead_of_filing(self, mod):
        rows = [_row(mod, issue_id="a", name="RecurringWarning",
                     description="Selector miss: Comment sort control")]
        matches = {mod.marker("a"): {"number": 818, "title": "t", "signature": "Selector miss"}}
        actions = mod.plan_actions(rows, set(), existing_matches=matches)
        assert actions[0]["action"] == "comment"
        assert actions[0]["existing"]["number"] == 818
        assert mod.pending(actions) == actions

    def test_the_marker_still_wins_over_a_text_match(self, mod):
        rows = [_row(mod, issue_id="a", name="RecurringWarning",
                     description="Selector miss: Comment sort control")]
        matches = {mod.marker("a"): {"number": 818, "title": "t"}}
        actions = mod.plan_actions(rows, {mod.marker("a")}, existing_matches=matches)
        assert actions[0]["action"] == "skip"
        assert actions[0]["reason"] == "already filed"

    def test_comments_do_not_spend_the_max_new_budget(self, mod):
        rows = [_row(mod, issue_id="a", name="RecurringWarning", description="Selector miss: one"),
                _row(mod, issue_id="b"), _row(mod, issue_id="c")]
        matches = {mod.marker("a"): {"number": 818, "title": "t"}}
        actions = mod.plan_actions(rows, set(), max_new=1, existing_matches=matches)
        assert [a["action"] for a in actions] == ["comment", "create", "deferred"]

    def test_summarize_counts_the_outcomes(self, mod):
        actions = mod.plan_actions([_row(mod, issue_id="a"),
                                    _row(mod, issue_id="b", status="resolved")], set())
        assert "1 create" in mod.summarize(actions)
        assert "1 skip" in mod.summarize(actions)

    def test_summarize_on_an_empty_window(self, mod):
        assert "no error-tracking issues" in mod.summarize([])


class TestPostHogQueryClientRetry:
    """Covers the 2026-08-31 08:30 UTC outage fix.

    A transient 503 from the query endpoint killed the whole run with no retry, and the cron's
    daily 24h lookback meant that window was gone from every future run. Retry a 5xx/connection
    failure, never a 4xx.
    """

    def _response(self, status_code=200, json_payload=None, ok=True):
        response = MagicMock()
        response.status_code = status_code
        response.json.return_value = json_payload if json_payload is not None else {}
        if ok:
            response.raise_for_status.return_value = None
        else:
            error = requests.HTTPError(f"{status_code} error")
            error.response = response
            response.raise_for_status.side_effect = error
        return response

    def test_a_503_then_200_succeeds_and_returns_the_rows(self, mod):
        client = mod.PostHogQueryClient("key", "475262")
        bad = self._response(status_code=503, ok=False)
        good = self._response(status_code=200, json_payload={"results": [["a"]]})
        with patch("requests.post", side_effect=[bad, good]) as post, \
                patch("time.sleep") as sleep:
            rows = client.query("SELECT 1")
        assert rows == [["a"]]
        assert post.call_count == 2
        sleep.assert_called_once()

    def test_repeated_5xx_exhausts_retries_and_still_raises(self, mod):
        client = mod.PostHogQueryClient("key", "475262")
        bad = self._response(status_code=503, ok=False)
        with patch("requests.post", return_value=bad) as post, patch("time.sleep"):
            with pytest.raises(requests.HTTPError):
                client.query("SELECT 1")
        assert post.call_count == mod._QUERY_RETRY_ATTEMPTS

    def test_a_4xx_is_never_retried(self, mod):
        client = mod.PostHogQueryClient("key", "475262")
        bad = self._response(status_code=400, ok=False)
        with patch("requests.post", return_value=bad) as post, patch("time.sleep") as sleep:
            with pytest.raises(requests.HTTPError):
                client.query("SELECT 1")
        assert post.call_count == 1
        sleep.assert_not_called()

    def test_a_connection_error_is_retried(self, mod):
        client = mod.PostHogQueryClient("key", "475262")
        good = self._response(status_code=200, json_payload={"results": []})
        with patch("requests.post",
                   side_effect=[requests.ConnectionError("refused"), good]) as post, \
                patch("time.sleep"):
            rows = client.query("SELECT 1")
        assert rows == []
        assert post.call_count == 2

    def test_a_timeout_is_retried(self, mod):
        client = mod.PostHogQueryClient("key", "475262")
        good = self._response(status_code=200, json_payload={"results": []})
        with patch("requests.post", side_effect=[requests.Timeout("slow"), good]) as post, \
                patch("time.sleep"):
            rows = client.query("SELECT 1")
        assert rows == []
        assert post.call_count == 2

    def test_main_still_exits_nonzero_after_retries_are_exhausted(self, mod, monkeypatch):
        # The retry wrapper must not swallow an unrecoverable failure into a fake "0 issues" run.
        monkeypatch.setenv("POSTHOG_QUERY_API_KEY", "key")
        bad = self._response(status_code=503, ok=False)
        with patch("requests.post", return_value=bad), patch("time.sleep"), \
                patch.object(mod, "GitHubIssues"):
            rc = mod.main(["--apply"])
        assert rc == 1


class TestGitHubIssues:
    def _completed(self, stdout="", returncode=0, stderr=""):
        return SimpleNamespace(stdout=stdout, returncode=returncode, stderr=stderr)

    def test_is_filed_requires_the_literal_marker_in_the_body(self, mod):
        gh = mod.GitHubIssues("owner/repo")
        # GitHub's tokenized search can return a NEIGHBOURING issue; it must not count as filed.
        with patch.object(gh, "_run", return_value=self._completed(
                '[{"number": 4, "body": "posthog-issue-someone-else"}]')):
            assert gh.is_filed("posthog-issue-a") is False

        with patch.object(gh, "_run", return_value=self._completed(
                '[{"number": 4, "body": "marker: posthog-issue-a here"}]')):
            assert gh.is_filed("posthog-issue-a") is True

    def test_is_filed_raises_when_the_search_fails(self, mod):
        gh = mod.GitHubIssues("owner/repo")
        with patch.object(gh, "_run", return_value=self._completed(returncode=1, stderr="401")):
            with pytest.raises(RuntimeError):
                gh.is_filed("posthog-issue-a")

    def test_is_filed_raises_on_unparseable_output(self, mod):
        gh = mod.GitHubIssues("owner/repo")
        with patch.object(gh, "_run", return_value=self._completed("not json")):
            with pytest.raises(RuntimeError):
                gh.is_filed("posthog-issue-a")

    def test_is_filed_sees_a_marker_left_as_a_comment(self, mod):
        # #1083: when the marker landed on a hand-filed tracker as a comment, that thread IS this
        # exception's issue — a body-only check would file a duplicate on the next run.
        gh = mod.GitHubIssues("owner/repo")
        with patch.object(gh, "_run", return_value=self._completed(
                '[{"number": 818, "body": "hand written", '
                '"comments": [{"body": "marker: posthog-issue-a"}]}]')):
            assert gh.is_filed("posthog-issue-a") is True

    def test_is_filed_tolerates_an_issue_with_no_comments(self, mod):
        gh = mod.GitHubIssues("owner/repo")
        with patch.object(gh, "_run", return_value=self._completed(
                '[{"number": 818, "body": "hand written", "comments": null}]')):
            assert gh.is_filed("posthog-issue-a") is False

    def test_search_open_quotes_the_phrase_and_stays_on_open_issues(self, mod):
        gh = mod.GitHubIssues("owner/repo")
        with patch.object(gh, "_run", return_value=self._completed(
                '[{"number": 818, "title": "t", "body": "b"}]')) as run:
            assert gh.search_open("Selector miss: Comment sort control")[0]["number"] == 818
        args = run.call_args[0][0]
        assert "--state" in args and args[args.index("--state") + 1] == "open"
        assert '"Selector miss: Comment sort control"' in args

    def test_search_open_skips_the_call_for_an_empty_signature(self, mod):
        gh = mod.GitHubIssues("owner/repo")
        with patch.object(gh, "_run") as run:
            assert gh.search_open("   ") == []
        run.assert_not_called()

    def test_search_open_raises_when_the_search_fails(self, mod):
        # Fail CLOSED — an unreadable search must not be read as "nothing tracks this yet".
        gh = mod.GitHubIssues("owner/repo")
        with patch.object(gh, "_run", return_value=self._completed(returncode=1, stderr="502")):
            with pytest.raises(RuntimeError):
                gh.search_open("Selector miss: Comment sort control")

    def test_comment_returns_the_comment_url(self, mod):
        gh = mod.GitHubIssues("owner/repo")
        with patch.object(gh, "_run", return_value=self._completed(
                "https://github.com/owner/repo/issues/818#issuecomment-1\n")) as run:
            assert gh.comment(818, "body").endswith("#issuecomment-1")
        args = run.call_args[0][0]
        assert args[:3] == ["gh", "issue", "comment"] and "818" in args

    def test_comment_returns_none_when_gh_fails(self, mod):
        gh = mod.GitHubIssues("owner/repo")
        with patch.object(gh, "_run", return_value=self._completed(returncode=1, stderr="boom")):
            assert gh.comment(818, "body") is None

    def test_create_returns_the_issue_url(self, mod):
        gh = mod.GitHubIssues("owner/repo")
        with patch.object(gh, "_run", return_value=self._completed(
                "https://github.com/owner/repo/issues/9\n")) as run:
            assert gh.create("t", "b") == "https://github.com/owner/repo/issues/9"
        args = run.call_args[0][0]
        assert "--label" in args and "agent:ready" in args and "bug" in args

    def test_create_returns_none_when_gh_fails(self, mod):
        gh = mod.GitHubIssues("owner/repo")
        with patch.object(gh, "_run", return_value=self._completed(returncode=1, stderr="boom")):
            assert gh.create("t", "b") is None


class TestApplyActions:
    def test_dry_run_files_nothing(self, mod):
        gh = MagicMock()
        actions = mod.plan_actions([_row(mod, issue_id="a")], set())
        actions[0]["body"] = "body"
        mod.apply_actions(gh, actions, dry_run=True)
        gh.create.assert_not_called()

    def test_apply_files_each_pending_issue(self, mod):
        gh = MagicMock()
        gh.create.return_value = "https://github.com/o/r/issues/1"
        actions = mod.plan_actions([_row(mod, issue_id="a")], set())
        actions[0]["body"] = "body"
        applied = mod.apply_actions(gh, actions, dry_run=False)
        assert len(applied) == 1
        gh.create.assert_called_once()

    def _comment_actions(self, mod):
        rows = [_row(mod, issue_id="a", name="RecurringWarning",
                     description="Selector miss: Comment sort control")]
        actions = mod.plan_actions(rows, set(), existing_matches={
            mod.marker("a"): {"number": 818, "title": "t"}})
        actions[0]["body"] = "occurrence report"
        return actions

    def test_a_matched_warning_is_commented_never_created(self, mod):
        gh = MagicMock()
        gh.comment.return_value = "https://github.com/o/r/issues/818#issuecomment-1"
        applied = mod.apply_actions(gh, self._comment_actions(mod), dry_run=False)
        gh.create.assert_not_called()
        gh.comment.assert_called_once_with(818, "occurrence report")
        assert len(applied) == 1

    def test_dry_run_comments_nothing(self, mod):
        gh = MagicMock()
        mod.apply_actions(gh, self._comment_actions(mod), dry_run=True)
        gh.comment.assert_not_called()
        gh.create.assert_not_called()

    def test_a_failed_comment_is_not_counted_as_applied(self, mod):
        gh = MagicMock()
        gh.comment.return_value = None
        assert mod.apply_actions(gh, self._comment_actions(mod), dry_run=False) == []

    def test_a_failed_create_is_not_counted_as_applied(self, mod):
        gh = MagicMock()
        gh.create.return_value = None
        actions = mod.plan_actions([_row(mod, issue_id="a")], set())
        actions[0]["body"] = "body"
        assert mod.apply_actions(gh, actions, dry_run=False) == []


class TestMain:
    def test_print_sql_needs_no_network(self, mod, capsys):
        assert mod.main(["--print-sql"]) == 0
        assert "$exception" in capsys.readouterr().out

    def test_missing_api_key_is_an_error(self, mod, monkeypatch, capsys):
        monkeypatch.delenv("POSTHOG_PERSONAL_API_KEY", raising=False)
        assert mod.main([]) == 1
        err = capsys.readouterr().err
        # Names BOTH vars — the reader has to know which one to set (issue #1453).
        assert "POSTHOG_QUERY_API_KEY" in err and "POSTHOG_PERSONAL_API_KEY" in err

    def test_the_query_scoped_key_outranks_the_shared_one(self, mod, monkeypatch):
        monkeypatch.setenv("POSTHOG_PERSONAL_API_KEY", "phx_shared")
        monkeypatch.setenv("POSTHOG_QUERY_API_KEY", "phx_query")
        with patch.object(mod, "PostHogQueryClient") as client:
            client.return_value.query.return_value = []
            assert mod.main([]) == 0
        assert client.call_args.args[0] == "phx_query"

    def test_the_shared_key_still_works_alone(self, mod, monkeypatch):
        # Additive rollout: the cron keeps filing before the scoped key exists.
        monkeypatch.setenv("POSTHOG_PERSONAL_API_KEY", "phx_shared")
        monkeypatch.delenv("POSTHOG_QUERY_API_KEY", raising=False)
        with patch.object(mod, "PostHogQueryClient") as client:
            client.return_value.query.return_value = []
            assert mod.main([]) == 0
        assert client.call_args.args[0] == "phx_shared"

    def test_another_purpose_s_key_is_still_an_error(self, mod, monkeypatch):
        monkeypatch.delenv("POSTHOG_PERSONAL_API_KEY", raising=False)
        monkeypatch.setenv("POSTHOG_RUNTIME_API_KEY", "phx_runtime")
        with patch.object(mod, "PostHogQueryClient") as client:
            assert mod.main([]) == 1
        client.assert_not_called()

    def test_dry_run_reports_pending_with_exit_2(self, mod, monkeypatch):
        monkeypatch.setenv("POSTHOG_PERSONAL_API_KEY", "phx_test")
        with patch.object(mod.PostHogQueryClient, "query",
                          return_value=[["a", "RuntimeError", "boom", "active", "t0", "t1", 3, 1,
                                         "posthog-python", "automate_commenting", None]]), \
             patch.object(mod.GitHubIssues, "is_filed", return_value=False), \
             patch.object(mod.GitHubIssues, "create") as create:
            assert mod.main([]) == 2
        create.assert_not_called()

    def test_apply_files_and_exits_zero(self, mod, monkeypatch):
        monkeypatch.setenv("POSTHOG_PERSONAL_API_KEY", "phx_test")
        with patch.object(mod.PostHogQueryClient, "query",
                          return_value=[["a", "RuntimeError", "boom", "active", "t0", "t1", 3, 1,
                                         "posthog-python", "automate_commenting", None]]), \
             patch.object(mod.GitHubIssues, "is_filed", return_value=False), \
             patch.object(mod.GitHubIssues, "create",
                          return_value="https://github.com/o/r/issues/1") as create:
            assert mod.main(["--apply"]) == 0
        create.assert_called_once()

    def test_nothing_pending_exits_zero(self, mod, monkeypatch):
        monkeypatch.setenv("POSTHOG_PERSONAL_API_KEY", "phx_test")
        with patch.object(mod.PostHogQueryClient, "query", return_value=[]), \
             patch.object(mod.GitHubIssues, "create") as create:
            assert mod.main([]) == 0
        create.assert_not_called()

    def test_a_posthog_failure_exits_one_without_filing(self, mod, monkeypatch):
        monkeypatch.setenv("POSTHOG_PERSONAL_API_KEY", "phx_test")
        with patch.object(mod.PostHogQueryClient, "query", side_effect=RuntimeError("500")), \
             patch.object(mod.GitHubIssues, "create") as create:
            assert mod.main(["--apply"]) == 1
        create.assert_not_called()

    _WARNING_ROW = [["a", "RecurringWarning", "Selector miss: Comment sort control", "active",
                     "t0", "t1", 3, 1, "posthog-python", "sweep_comment_outcomes", None]]

    def test_a_hand_filed_tracker_gets_a_comment_not_a_duplicate(self, mod, monkeypatch):
        # The #1063/#818 case end to end: an open hand-filed issue quotes the warning, so nothing
        # new is opened.
        monkeypatch.setenv("POSTHOG_PERSONAL_API_KEY", "phx_test")
        with patch.object(mod.PostHogQueryClient, "query", return_value=self._WARNING_ROW), \
             patch.object(mod.GitHubIssues, "is_filed", return_value=False), \
             patch.object(mod.GitHubIssues, "search_open", return_value=[
                 {"number": 818,
                  "title": "fix(observability): 'Selector miss: Comment sort control' — denominator",
                  "body": "hand written"}]), \
             patch.object(mod.GitHubIssues, "comment",
                          return_value="https://github.com/o/r/issues/818#issuecomment-1") as comment, \
             patch.object(mod.GitHubIssues, "create") as create:
            assert mod.main(["--apply"]) == 0
        create.assert_not_called()
        number, body = comment.call_args[0]
        assert number == 818
        assert mod.marker("a") in body

    def test_an_unmatched_warning_still_files(self, mod, monkeypatch):
        monkeypatch.setenv("POSTHOG_PERSONAL_API_KEY", "phx_test")
        with patch.object(mod.PostHogQueryClient, "query", return_value=self._WARNING_ROW), \
             patch.object(mod.GitHubIssues, "is_filed", return_value=False), \
             patch.object(mod.GitHubIssues, "search_open", return_value=[]), \
             patch.object(mod.GitHubIssues, "comment") as comment, \
             patch.object(mod.GitHubIssues, "create",
                          return_value="https://github.com/o/r/issues/1") as create:
            assert mod.main(["--apply"]) == 0
        comment.assert_not_called()
        create.assert_called_once()

    def test_a_failed_signature_search_exits_one_without_writing(self, mod, monkeypatch):
        monkeypatch.setenv("POSTHOG_PERSONAL_API_KEY", "phx_test")
        with patch.object(mod.PostHogQueryClient, "query", return_value=self._WARNING_ROW), \
             patch.object(mod.GitHubIssues, "is_filed", return_value=False), \
             patch.object(mod.GitHubIssues, "search_open", side_effect=RuntimeError("gh down")), \
             patch.object(mod.GitHubIssues, "comment") as comment, \
             patch.object(mod.GitHubIssues, "create") as create:
            assert mod.main(["--apply"]) == 1
        create.assert_not_called()
        comment.assert_not_called()

    def test_a_github_lookup_failure_exits_one_without_filing(self, mod, monkeypatch):
        monkeypatch.setenv("POSTHOG_PERSONAL_API_KEY", "phx_test")
        with patch.object(mod.PostHogQueryClient, "query",
                          return_value=[["a", "RuntimeError", "boom", "active", "t0", "t1", 3, 1,
                                         "posthog-python", None, None]]), \
             patch.object(mod.GitHubIssues, "is_filed", side_effect=RuntimeError("gh down")), \
             patch.object(mod.GitHubIssues, "create") as create:
            assert mod.main(["--apply"]) == 1
        create.assert_not_called()
