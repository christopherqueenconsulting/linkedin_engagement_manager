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
        07:10:54 — head was compared two-deep against a three-category base."""
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
