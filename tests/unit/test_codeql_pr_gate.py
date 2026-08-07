"""Unit tests for scripts/codeql_pr_gate.py — the per-PR CodeQL alert diff.

Focus is issue #904: "an analysis exists for this ref" is not "an analysis exists for
THIS commit". The old wait returned instantly on every push after the first, so the gate
diffed the previous commit's alerts — flagging issues the push had already fixed, and
silently missing new ones whenever the previous commit happened to be clean.
"""

import importlib.util
import pathlib
import sys
from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.unit

_SCRIPTS = pathlib.Path(__file__).resolve().parents[2] / "scripts"


def _load(name):
    spec = importlib.util.spec_from_file_location(name, _SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    # @dataclass resolves annotations through sys.modules[cls.__module__].
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


gate = _load("codeql_pr_gate")

NEW_SHA = "31496b31aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
OLD_SHA = "6f28084636b2b8ea0b9f1085cda79b83ec501fa8"
PY = "/language:python"
PY_ADVANCED = "/language:python/advanced"
JS = "/language:javascript-typescript"


def _analysis(commit_sha: str, category: str) -> dict:
    return {"commit_sha": commit_sha, "category": category,
            "created_at": "2026-08-02T01:44:22Z"}


class _FakeClient:
    """Returns a different /analyses page per poll, then repeats the last one.

    ``base_pages`` answers any ref other than ``ref`` (the base-ref calibration lookup).
    """

    def __init__(self, pages: list[list[dict]], base: list[dict] | None = None) -> None:
        self.pages = pages
        self.base = base
        self.calls = 0
        self.base_calls = 0

    def list_analyses(self, ref: str) -> list[dict]:
        if self.base is not None and ref.startswith("refs/heads/"):
            self.base_calls += 1
            return self.base
        self.calls += 1
        return self.pages[min(self.calls - 1, len(self.pages) - 1)]


class TestExpectedCategories:
    def test_reads_the_newest_other_commit(self):
        analyses = [
            _analysis(NEW_SHA, PY),
            _analysis(OLD_SHA, PY),
            _analysis(OLD_SHA, JS),
        ]
        assert gate.expected_categories(analyses, NEW_SHA, lookback=1) == {PY, JS}

    def test_takes_the_largest_set_not_the_newest(self):
        """A previous commit whose run only half-uploaded must not lower the bar."""
        analyses = [
            _analysis(NEW_SHA, PY),
            _analysis(OLD_SHA, JS),  # newest other commit — partial
            _analysis("evenolder", PY),
            _analysis("evenolder", JS),
            _analysis("evenolder", PY_ADVANCED),
        ]
        assert gate.expected_categories(analyses, NEW_SHA) == {PY, JS, PY_ADVANCED}

    def test_lookback_bounds_how_far_back_it_calibrates(self):
        analyses = [
            _analysis(OLD_SHA, JS),
            _analysis("c2", JS),
            _analysis("c3", JS),
            _analysis("c4", PY),
            _analysis("c4", JS),
            _analysis("c4", PY_ADVANCED),
        ]
        assert gate.expected_categories(analyses, NEW_SHA, lookback=3) == {JS}

    def test_empty_on_a_prs_first_push(self):
        analyses = [_analysis(NEW_SHA, PY)]
        assert gate.expected_categories(analyses, NEW_SHA) == set()

    def test_ignores_entries_without_a_commit_or_category(self):
        analyses = [{"category": PY}, {"commit_sha": OLD_SHA}, _analysis(OLD_SHA, JS)]
        assert gate.expected_categories(analyses, NEW_SHA) == {JS}


class TestWaitForAnalysis:
    def test_stale_analysis_for_an_older_commit_is_not_accepted(self):
        """The #904 bug: this used to return True immediately."""
        client = _FakeClient([[_analysis(OLD_SHA, PY), _analysis(OLD_SHA, JS)]])
        assert gate.wait_for_analysis(
            client, "refs/pull/899/merge", timeout=0, interval=0, commit_sha=NEW_SHA
        ) is False

    def test_returns_once_the_commits_analysis_lands(self):
        client = _FakeClient([
            [_analysis(OLD_SHA, PY)],
            [_analysis(NEW_SHA, PY), _analysis(OLD_SHA, PY)],
        ])
        assert gate.wait_for_analysis(
            client, "refs/pull/899/merge", timeout=60, interval=0, commit_sha=NEW_SHA
        ) is True
        assert client.calls == 2

    def test_waits_for_every_category_the_previous_commit_produced(self):
        """A partial upload is still a stale diff for the categories not yet in."""
        previous = [_analysis(OLD_SHA, PY), _analysis(OLD_SHA, JS)]
        client = _FakeClient([
            [_analysis(NEW_SHA, JS)] + previous,
            [_analysis(NEW_SHA, PY), _analysis(NEW_SHA, JS)] + previous,
        ])
        assert gate.wait_for_analysis(
            client, "refs/pull/899/merge", timeout=60, interval=0, commit_sha=NEW_SHA
        ) is True
        assert client.calls == 2

    def test_partial_upload_alone_times_out(self):
        previous = [_analysis(OLD_SHA, PY), _analysis(OLD_SHA, JS)]
        client = _FakeClient([[_analysis(NEW_SHA, JS)] + previous])
        assert gate.wait_for_analysis(
            client, "refs/pull/899/merge", timeout=0, interval=0, commit_sha=NEW_SHA
        ) is False

    def test_first_push_with_no_base_ref_accepts_what_it_has(self):
        client = _FakeClient([[_analysis(NEW_SHA, PY)]])
        assert gate.wait_for_analysis(
            client, "refs/pull/899/merge", timeout=0, interval=0, commit_sha=NEW_SHA
        ) is True

    def test_first_push_calibrates_off_the_base_ref(self):
        """The live hole this closes: on PR #913's own run the gate accepted at 07:10:36
        with javascript + python/advanced in, while /language:python landed at
        07:10:54 — head was compared two-deep against a three-category base.
        """
        base = [_analysis(OLD_SHA, PY), _analysis(OLD_SHA, JS),
                _analysis(OLD_SHA, PY_ADVANCED)]
        client = _FakeClient(
            [
                [_analysis(NEW_SHA, JS), _analysis(NEW_SHA, PY_ADVANCED)],
                [_analysis(NEW_SHA, JS), _analysis(NEW_SHA, PY_ADVANCED),
                 _analysis(NEW_SHA, PY)],
            ],
            base=base,
        )
        assert gate.wait_for_analysis(
            client, "refs/pull/913/merge", timeout=60, interval=0,
            commit_sha=NEW_SHA, base_ref="refs/heads/main",
        ) is True
        assert client.calls == 2
        # Calibration is a fixed property of the ref — fetched once, not once per poll.
        assert client.base_calls == 1

    def test_first_push_partial_upload_times_out_against_the_base_ref(self):
        base = [_analysis(OLD_SHA, PY), _analysis(OLD_SHA, JS),
                _analysis(OLD_SHA, PY_ADVANCED)]
        client = _FakeClient([[_analysis(NEW_SHA, JS)]], base=base)
        assert gate.wait_for_analysis(
            client, "refs/pull/913/merge", timeout=0, interval=0,
            commit_sha=NEW_SHA, base_ref="refs/heads/main",
        ) is False

    def test_unanalyzed_base_ref_does_not_block_the_gate(self):
        """No calibration source anywhere → fall back to accepting what landed."""
        client = _FakeClient([[_analysis(NEW_SHA, PY)]], base=[])
        assert gate.wait_for_analysis(
            client, "refs/pull/913/merge", timeout=0, interval=0,
            commit_sha=NEW_SHA, base_ref="refs/heads/main",
        ) is True

    def test_without_a_commit_sha_any_analysis_counts(self):
        """workflow_call passes a bare SHA as the ref; behaviour is unchanged there."""
        client = _FakeClient([[_analysis(OLD_SHA, PY)]])
        assert gate.wait_for_analysis(
            client, "refs/pull/899/merge", timeout=0, interval=0, commit_sha=""
        ) is True

    def test_no_analyses_at_all_times_out(self):
        client = _FakeClient([[]])
        assert gate.wait_for_analysis(
            client, "refs/pull/899/merge", timeout=0, interval=0, commit_sha=""
        ) is False


class TestListAnalyses:
    def test_returns_the_dicts_the_api_gave(self):
        client = gate.GitHubClient("token", "o/r")
        with patch.object(client, "get", return_value=[_analysis(NEW_SHA, PY), "junk"]):
            assert client.list_analyses("refs/heads/main") == [_analysis(NEW_SHA, PY)]

    def test_api_failure_is_an_empty_list_not_a_crash(self):
        client = gate.GitHubClient("token", "o/r")
        with patch.object(client, "get", return_value=None):
            assert client.list_analyses("refs/heads/main") == []


class TestMainStaleGuard:
    def _run(self, argv, list_analyses_return, monkeypatch, tmp_path):
        monkeypatch.setenv("GITHUB_TOKEN", "token")
        monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(tmp_path / "summary.md"))
        monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
        client = MagicMock()
        client.list_analyses.return_value = list_analyses_return
        client.fetch_alerts.return_value = []
        with patch.object(gate, "GitHubClient", return_value=client):
            return gate.main(argv), client

    def test_stale_ref_never_compares_and_fails_open(
        self, monkeypatch, tmp_path, capsys
    ):
        code, client = self._run(
            [
                "--repo", "o/r",
                "--head-ref", "refs/pull/899/merge",
                "--head-sha", NEW_SHA,
                "--base-ref", "refs/heads/main",
                "--wait-timeout", "0",
                "--wait-interval", "0",
            ],
            [_analysis(OLD_SHA, PY), _analysis(OLD_SHA, JS)],
            monkeypatch,
            tmp_path,
        )
        # Fail-open is deliberate: a slow CodeQL must not block every merge.
        assert code == 0
        client.fetch_alerts.assert_not_called()
        out = capsys.readouterr().out
        assert "CodeQL gate did not run" in out
        assert NEW_SHA in out

    def test_fresh_analysis_is_compared(self, monkeypatch, tmp_path):
        code, client = self._run(
            [
                "--repo", "o/r",
                "--head-ref", "refs/pull/899/merge",
                "--head-sha", NEW_SHA,
                "--base-ref", "refs/heads/main",
                "--wait-timeout", "0",
                "--wait-interval", "0",
            ],
            [_analysis(NEW_SHA, PY), _analysis(OLD_SHA, PY)],
            monkeypatch,
            tmp_path,
        )
        assert code == 0
        client.fetch_alerts.assert_any_call("refs/pull/899/merge")
        client.fetch_alerts.assert_any_call("refs/heads/main")


_MINIMAL_ARGV = [
    "--repo", "o/r",
    "--head-ref", "refs/pull/1/merge",
    "--base-ref", "refs/heads/main",
]


class TestArgs:
    def test_head_sha_defaults_to_empty(self):
        assert gate.parse_args(_MINIMAL_ARGV).head_sha == ""

    def test_wait_timeout_outlasts_a_real_codeql_run(self):
        # The wait is real now, so the timeout has to cover a CodeQL run + queue time.
        assert gate.parse_args(_MINIMAL_ARGV).wait_timeout >= 600


def _raw_alert(number: int, path: str, line: int, rule: str = "py/empty-except") -> dict:
    return {
        "number": number,
        "rule": {"id": rule, "severity": "note", "security_severity_level": None},
        "html_url": f"https://example.invalid/{number}",
        "most_recent_instance": {
            "location": {"path": path, "start_line": line, "end_line": line},
            "message": {"text": "same finding"},
        },
    }


class TestFindNewAlerts:
    """The gate keys alerts on (rule, path, LINE). A PR that adds code ABOVE a pre-existing alert
    moves its line, which read as a NEW alert and failed the gate on debt the PR did not create —
    deterministically, on every merge_group run, which evicted the PR from the merge queue."""

    def _alerts(self, raws):
        return [a for a in (gate._alert_from_raw(r) for r in raws) if a]

    def test_a_line_shifted_pre_existing_alert_is_not_new(self):
        base = self._alerts([_raw_alert(2623, "src/a.py", 4240)])
        head = self._alerts([_raw_alert(2623, "src/a.py", 4253)])
        assert gate.find_new_alerts(head, base) == []

    def test_a_genuinely_new_alert_still_fails_the_gate(self):
        base = self._alerts([_raw_alert(2623, "src/a.py", 4240)])
        head = self._alerts([_raw_alert(2623, "src/a.py", 4253),
                             _raw_alert(9001, "src/b.py", 12)])
        assert [a.number for a in gate.find_new_alerts(head, base)] == [9001]

    def test_an_unchanged_alert_is_not_new(self):
        base = self._alerts([_raw_alert(2623, "src/a.py", 10)])
        head = self._alerts([_raw_alert(2623, "src/a.py", 10)])
        assert gate.find_new_alerts(head, base) == []

    def test_the_line_key_still_catches_an_alert_carrying_no_number(self):
        """Number matching is the addition, not the replacement: an alert that arrives without one
        must still be judged on its location."""
        base = self._alerts([_raw_alert(0, "src/a.py", 10)])
        head = self._alerts([_raw_alert(0, "src/b.py", 99)])
        assert len(gate.find_new_alerts(head, base)) == 1


class TestCompareAlerts:
    """Matching by number is the only way this gate can over-match, so the count is reported."""

    def _alerts(self, raws):
        return [a for a in (gate._alert_from_raw(r) for r in raws) if a]

    def test_a_line_shifted_alert_is_counted_as_shift_matched(self):
        base = self._alerts([_raw_alert(2623, "src/a.py", 4240)])
        head = self._alerts([_raw_alert(2623, "src/a.py", 4253)])
        comparison = gate.compare_alerts(head, base)
        assert (comparison.shift_matched, comparison.exact_matched) == (1, 0)
        assert comparison.new_alerts == []

    def test_an_unmoved_alert_is_an_exact_match_not_a_shift(self):
        base = self._alerts([_raw_alert(2623, "src/a.py", 10)])
        head = self._alerts([_raw_alert(2623, "src/a.py", 10)])
        comparison = gate.compare_alerts(head, base)
        assert (comparison.shift_matched, comparison.exact_matched) == (0, 1)

    def test_a_new_alert_is_matched_by_neither_pass(self):
        base = self._alerts([_raw_alert(2623, "src/a.py", 4240)])
        head = self._alerts([_raw_alert(2623, "src/a.py", 4253),
                             _raw_alert(9001, "src/b.py", 12)])
        comparison = gate.compare_alerts(head, base)
        assert (comparison.shift_matched, comparison.exact_matched) == (1, 0)
        assert [a.number for a in comparison.new_alerts] == [9001]


class TestComparedAlertsLog:
    """Acceptance for #1087: the shift-tolerant pass must never be silent."""

    def _run(self, head_raws, base_raws, monkeypatch, tmp_path):
        monkeypatch.setenv("GITHUB_TOKEN", "token")
        monkeypatch.setenv("GITHUB_OUTPUT", str(tmp_path / "outputs.txt"))
        client = MagicMock()
        client.list_analyses.return_value = [_analysis(NEW_SHA, PY)]
        alerts = {
            "refs/pull/1067/merge": [a for a in (gate._alert_from_raw(r) for r in head_raws) if a],
            "refs/heads/main": [a for a in (gate._alert_from_raw(r) for r in base_raws) if a],
        }
        client.fetch_alerts.side_effect = lambda ref: alerts[ref]
        with patch.object(gate, "GitHubClient", return_value=client), \
                patch.object(gate, "log_info") as log_info:
            code = gate.main([
                "--repo", "o/r",
                "--head-ref", "refs/pull/1067/merge",
                "--head-sha", NEW_SHA,
                "--base-ref", "refs/heads/main",
                "--wait-timeout", "0",
                "--wait-interval", "0",
            ])
        compared = [c for c in log_info.call_args_list if c.args and c.args[0] == "Compared alerts"]
        assert len(compared) == 1
        return code, compared[0].kwargs, (tmp_path / "outputs.txt").read_text()

    def test_the_shift_matched_count_is_reported(self, monkeypatch, tmp_path):
        # PR #1067's real shape: 13 lines added above a pre-existing alert, nothing new.
        code, kwargs, outputs = self._run(
            [_raw_alert(2623, "src/a.py", 4253)],
            [_raw_alert(2623, "src/a.py", 4240)],
            monkeypatch,
            tmp_path,
        )
        assert code == 0
        assert kwargs["new_count"] == 0
        assert kwargs["shift_matched"] == 1
        assert kwargs["exact_matched"] == 0
        assert "shift_matched_count=1" in outputs

    def test_a_run_with_nothing_shifted_reports_zero(self, monkeypatch, tmp_path):
        code, kwargs, outputs = self._run(
            [_raw_alert(2623, "src/a.py", 10)],
            [_raw_alert(2623, "src/a.py", 10)],
            monkeypatch,
            tmp_path,
        )
        assert code == 0
        assert kwargs["shift_matched"] == 0
        assert kwargs["exact_matched"] == 1
        assert "shift_matched_count=0" in outputs

    def test_a_genuinely_new_alert_still_fails_the_gate(self, monkeypatch, tmp_path):
        code, kwargs, outputs = self._run(
            [_raw_alert(2623, "src/a.py", 4253), _raw_alert(9001, "src/b.py", 12)],
            [_raw_alert(2623, "src/a.py", 4240)],
            monkeypatch,
            tmp_path,
        )
        assert code == 1
        assert kwargs["new_count"] == 1
        assert kwargs["shift_matched"] == 1
        # The failing run is the one whose over-match matters most — a gate that blocks a PR
        # while having dismissed alerts on number alone must still say how many.
        assert "shift_matched_count=1" in outputs
