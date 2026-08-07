"""Unit tests for scripts/posthog_error_issues.py — the PostHog-issue -> GitHub-issue filer that
replaces the log-grep cron (issue #648).
"""

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

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

    def test_summarize_counts_the_outcomes(self, mod):
        actions = mod.plan_actions([_row(mod, issue_id="a"),
                                    _row(mod, issue_id="b", status="resolved")], set())
        assert "1 create" in mod.summarize(actions)
        assert "1 skip" in mod.summarize(actions)

    def test_summarize_on_an_empty_window(self, mod):
        assert "no error-tracking issues" in mod.summarize([])


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

    def test_missing_api_key_is_an_error(self, mod, monkeypatch):
        monkeypatch.delenv("POSTHOG_PERSONAL_API_KEY", raising=False)
        assert mod.main([]) == 1

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

    def test_a_github_lookup_failure_exits_one_without_filing(self, mod, monkeypatch):
        monkeypatch.setenv("POSTHOG_PERSONAL_API_KEY", "phx_test")
        with patch.object(mod.PostHogQueryClient, "query",
                          return_value=[["a", "RuntimeError", "boom", "active", "t0", "t1", 3, 1,
                                         "posthog-python", None, None]]), \
             patch.object(mod.GitHubIssues, "is_filed", side_effect=RuntimeError("gh down")), \
             patch.object(mod.GitHubIssues, "create") as create:
            assert mod.main(["--apply"]) == 1
        create.assert_not_called()
