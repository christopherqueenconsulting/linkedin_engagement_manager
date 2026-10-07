"""The media-benchmark scheduler (issue #2251).

Covers scripts/model_eval_loop.py and its wiring in scripts/weekly_model_check.sh. Git, gh and the
benchmark subprocess are all mocked.
"""

import json
import pathlib
import re
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.unit

_ROOT = pathlib.Path(__file__).resolve().parents[3]
_SCRIPTS = _ROOT / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import model_eval_loop as loop  # noqa: E402

BLOBS = {".litellm/config.yaml": "aaa", ".litellm/model_upgrades.yaml": "bbb"}


def _pr(number, sha="s1", association="OWNER", cross=False,
        files=(".litellm/config.yaml",)):
    return {"number": number, "sha": sha, "association": association, "cross_repo": cross,
            "files": list(files)}


class TestPlanTriggers:
    def test_first_run_is_the_monthly_run(self):
        plan = loop.plan_triggers({}, month="2026-10", main_blobs=BLOBS, prs=[], limit=2)
        assert plan == {"main": ["monthly"], "prs": [], "skipped": []}

    def test_same_month_same_blobs_is_not_due(self):
        state = {"month": "2026-10", "main_blobs": BLOBS}
        assert loop.plan_triggers(state, month="2026-10", main_blobs=BLOBS, prs=[],
                                  limit=2)["main"] == []

    def test_a_config_change_on_main_is_due(self):
        state = {"month": "2026-10", "main_blobs": BLOBS}
        moved = dict(BLOBS, **{".litellm/model_upgrades.yaml": "ccc"})
        plan = loop.plan_triggers(state, month="2026-10", main_blobs=moved, prs=[], limit=2)
        assert plan["main"] == ["changed on main: .litellm/model_upgrades.yaml"]

    def test_pr_filtering_trust_fork_done_and_cap(self):
        state = {"month": "2026-10", "main_blobs": BLOBS, "prs": {"5": "done"}}
        prs = [_pr(1), _pr(2, association="NONE"), _pr(3, cross=True),
               _pr(4, files=("README.md",)), _pr(5, sha="done"), _pr(6, association="MEMBER"),
               _pr(7, association="COLLABORATOR")]
        plan = loop.plan_triggers(state, month="2026-10", main_blobs=BLOBS, prs=prs, limit=2)
        assert plan["prs"] == [{"number": 1, "sha": "s1"}, {"number": 6, "sha": "s1"}]
        skipped = " ".join(plan["skipped"])
        assert "#2: author association 'NONE' not trusted" in skipped
        assert "#3: fork PR" in skipped
        assert "#7: over the per-run PR cap (2)" in skipped
        assert "#4" not in skipped and "#5" not in skipped

    def test_an_unreadable_listing_runs_no_pr(self):
        plan = loop.plan_triggers({}, month="2026-10", main_blobs=BLOBS, prs=None, limit=2)
        assert plan["prs"] == [] and "unreadable" in plan["skipped"][0]


class TestState:
    def test_month_advances_only_when_main_measured(self):
        skipped = loop.next_state({}, month="2026-10", main_blobs=BLOBS, ran_main=False,
                                  done_prs={}, open_numbers=None)
        assert skipped["month"] is None and skipped["main_blobs"] == BLOBS
        ran = loop.next_state({"month": "2026-09", "main_blobs": {"x": "y"}}, month="2026-10",
                              main_blobs=BLOBS, ran_main=True, done_prs={}, open_numbers=None)
        assert ran["month"] == "2026-10" and ran["main_blobs"] == BLOBS
        kept = loop.next_state({"main_blobs": {"x": "y"}}, month="2026-10", main_blobs=BLOBS,
                               ran_main=False, done_prs={}, open_numbers=None)
        assert kept["main_blobs"] == {"x": "y"}  # a missed run is retried, not marked done

    def test_prs_recorded_and_closed_ones_pruned(self):
        state = {"prs": {"1": "old", "2": "x"}}
        out = loop.next_state(state, month="m", main_blobs=BLOBS, ran_main=False,
                              done_prs={3: "s3"}, open_numbers={2, 3})
        assert out["prs"] == {"2": "x", "3": "s3"}

    def test_max_pr_runs_env(self, monkeypatch):
        monkeypatch.delenv("MODEL_EVAL_MAX_PR_RUNS", raising=False)
        assert loop.max_pr_runs() == 2
        monkeypatch.setenv("MODEL_EVAL_MAX_PR_RUNS", "0")
        assert loop.max_pr_runs() == 0
        monkeypatch.setenv("MODEL_EVAL_MAX_PR_RUNS", "x")
        assert loop.max_pr_runs() == 2


class TestComment:
    def test_comment_is_advisory_and_carries_the_report(self):
        body = loop.pr_comment_body(9, "abcdef1234567890", 2, "# Report", "line\n" * 40)
        assert body.startswith(loop.COMMENT_MARKER)
        assert "`abcdef1234`" in body and "recommends a swap" in body
        assert "NOT a check and never blocks a merge" in body
        assert "# Report" in body and body.count("line") == 25

    def test_refused_and_plain_statuses(self):
        assert "did not run to a verdict" in loop.pr_comment_body(1, "s" * 12, 1, None, "")
        assert "no swap recommended" in loop.pr_comment_body(1, "s" * 12, 0, None, "")


def _completed(rc=0, out="", err=""):
    return SimpleNamespace(returncode=rc, stdout=out, stderr=err)


class TestIO:
    def test_git_helpers(self):
        with patch.object(loop, "_run", return_value=_completed(0, "abc\n")):
            assert loop.git_blob("/r", "origin/main", "p") == "abc"
            assert loop.git_show("/r", "origin/main", "p") == "abc\n"
        with patch.object(loop, "_run", return_value=_completed(128)):
            assert loop.git_blob("/r", "origin/main", "p") is None
            assert loop.git_show("/r", "origin/main", "p") is None

    def test_list_open_prs(self):
        pulls = json.dumps([{"number": 4, "author_association": "OWNER",
                             "head": {"sha": "s4", "repo": {"full_name": loop.DEFAULT_REPO}}},
                            {"number": 5, "author_association": "NONE",
                             "head": {"sha": "s5", "repo": {"full_name": "evil/fork"}}}])
        calls = [_completed(0, pulls), _completed(0, ".litellm/config.yaml\n"),
                 _completed(0, "README.md\n")]
        with patch.object(subprocess, "run", side_effect=calls):
            prs = loop.list_open_prs(loop.DEFAULT_REPO)
        assert prs == [{"number": 4, "sha": "s4", "association": "OWNER", "cross_repo": False,
                        "files": [".litellm/config.yaml"]},
                       {"number": 5, "sha": "s5", "association": "NONE", "cross_repo": True,
                        "files": ["README.md"]}]

    @pytest.mark.parametrize("calls", [
        [_completed(1, err="auth")], [_completed(0, "not json")],
        [_completed(0, json.dumps([{"number": 1, "head": {}}])), _completed(1)]])
    def test_list_open_prs_fails_closed(self, calls):
        with patch.object(subprocess, "run", side_effect=calls):
            assert loop.list_open_prs(loop.DEFAULT_REPO) is None

    def test_run_benchmark_builds_the_media_command(self, tmp_path):
        with patch.object(subprocess, "run", return_value=_completed(2, "o", "e")) as run:
            rc, output = loop.run_benchmark(str(tmp_path), config="c.yaml", results="r.json",
                                            out_dir="d")
        assert (rc, output) == (2, "oe")
        cmd = run.call_args.args[0]
        assert cmd[cmd.index("--tiers") + 1] == "lem-vision,lem-image"
        assert cmd[cmd.index("--prices") + 1].startswith(str(tmp_path))
        with patch.object(subprocess, "run", side_effect=subprocess.TimeoutExpired("x", 1)):
            assert loop.run_benchmark("/r", config="c", results="r", out_dir="d")[0] == 1

    def test_newest_report_ignores_the_readme(self, tmp_path):
        assert loop._newest_report(str(tmp_path)) is None
        (tmp_path / "README.md").write_text("readme")
        (tmp_path / "2026-10-07-mm-a.md").write_text("report")
        assert loop._newest_report(str(tmp_path)) == "report"


class TestMain:
    def _main(self, tmp_path, *, prs, bench, state=None, posted=0):
        state_path = tmp_path / "state.json"
        if state is not None:
            state_path.write_text(json.dumps(state))

        def fake_bench(repo_dir, *, config, results, out_dir):
            rc, write = bench
            if write:
                pathlib.Path(results).write_text("{}")
                pathlib.Path(out_dir).mkdir(parents=True, exist_ok=True)
                (pathlib.Path(out_dir) / "2026-10-07-mm.md").write_text("# report")
            return rc, "bench output"

        with patch.object(loop, "_run", return_value=_completed(0)), \
                patch.object(loop, "git_blob", side_effect=lambda r, ref, p: BLOBS[p]), \
                patch.object(loop, "git_show", return_value="model_list: []\n"), \
                patch.object(loop, "list_open_prs", return_value=prs), \
                patch.object(loop, "run_benchmark", side_effect=fake_bench) as run_bench, \
                patch.object(subprocess, "run", return_value=_completed(posted)) as gh:
            rc = loop.main(["--state", str(state_path), "--results-dir", str(tmp_path / "res"),
                            "--repo-dir", str(tmp_path), "--month", "2026-10"])
        return rc, json.loads(state_path.read_text()), run_bench, gh

    def test_monthly_main_run_publishes_and_advances(self, tmp_path, capsys):
        rc, state, run_bench, _ = self._main(tmp_path, prs=[], bench=(0, True))
        assert rc == 2 and state["month"] == "2026-10"
        assert capsys.readouterr().out.strip().endswith(".json")
        assert run_bench.call_count == 1

    def test_a_skipped_main_run_does_not_advance(self, tmp_path):
        rc, state, _, _ = self._main(tmp_path, prs=[], bench=(0, False))
        assert rc == 0 and state["month"] is None

    def test_a_failed_main_run_is_an_error(self, tmp_path):
        rc, state, _, _ = self._main(tmp_path, prs=[], bench=(1, False))
        assert rc == 1 and state["month"] is None

    def test_pr_run_comments_and_records_the_sha(self, tmp_path):
        rc, state, _, gh = self._main(tmp_path, prs=[_pr(12, sha="abc")], bench=(0, True),
                                      state={"month": "2026-10", "main_blobs": BLOBS})
        assert rc == 0 and state["prs"] == {"12": "abc"}
        cmd = gh.call_args.args[0]
        assert cmd[:4] == ["gh", "pr", "comment", "12"] and "--body-file" in cmd

    def test_a_failed_comment_is_retried_next_time(self, tmp_path):
        _, state, _, _ = self._main(tmp_path, prs=[_pr(12)], bench=(1, False), posted=1,
                                    state={"month": "2026-10", "main_blobs": BLOBS})
        assert state["prs"] == {}

    def test_disabled_benchmark_posts_nothing(self, tmp_path):
        state_in = {"month": "2026-10", "main_blobs": BLOBS}
        with patch.object(loop, "pr_comment_body") as body:
            def bench(repo_dir, *, config, results, out_dir):
                return 0, "BENCHMARK_ENABLED is not set — nothing to do"
            state_path = tmp_path / "s.json"
            state_path.write_text(json.dumps(state_in))
            with patch.object(loop, "_run", return_value=_completed(0)), \
                    patch.object(loop, "git_blob", side_effect=lambda r, ref, p: BLOBS[p]), \
                    patch.object(loop, "git_show", return_value="x"), \
                    patch.object(loop, "list_open_prs", return_value=[_pr(3)]), \
                    patch.object(loop, "run_benchmark", side_effect=bench):
                assert loop.main(["--state", str(state_path), "--results-dir",
                                  str(tmp_path), "--month", "2026-10"]) == 0
        body.assert_not_called()

    def test_plan_only_prints_json(self, tmp_path, capsys):
        (tmp_path / "s.json").write_text("not json")
        with patch.object(loop, "_run", return_value=_completed(0)), \
                patch.object(loop, "git_blob", return_value=None), \
                patch.object(loop, "list_open_prs", return_value=None):
            assert loop.main(["--state", str(tmp_path / "s.json"), "--results-dir",
                              str(tmp_path), "--plan-only"]) == 0
        plan = json.loads(capsys.readouterr().out)
        assert plan["main"] == ["monthly"] and plan["prs"] == []

    def test_unreadable_pr_config_is_skipped(self, tmp_path):
        with patch.object(loop, "_run", return_value=_completed(0)), \
                patch.object(loop, "git_blob", side_effect=lambda r, ref, p: BLOBS[p]), \
                patch.object(loop, "git_show", return_value=None), \
                patch.object(loop, "list_open_prs", return_value=[_pr(8)]), \
                patch.object(loop, "run_benchmark") as run_bench:
            state = tmp_path / "s.json"
            state.write_text(json.dumps({"month": "2026-10", "main_blobs": BLOBS}))
            assert loop.main(["--state", str(state), "--results-dir", str(tmp_path),
                              "--month", "2026-10"]) == 0
        run_bench.assert_not_called()


class TestOrchestratorWiring:
    """Static checks on scripts/weekly_model_check.sh.

    It shells out to docker and gh, so it is read, not run (same approach as
    test_weekly_model_check_keys.py).
    """

    SH = (_ROOT / "scripts" / "weekly_model_check.sh").read_text(encoding="utf-8")

    def test_the_loop_exit_code_is_not_swallowed_by_a_pipe(self):
        block = self.SH.split('"${PY[@]}" scripts/model_eval_loop.py', 1)[1].split("case", 1)[0]
        assert "| tail" not in block and "| head" not in block
        assert "MRC=$?" in block

    def test_provider_keys_are_sourced_without_posthog(self):
        lines = [ln for ln in self.SH.splitlines() if "OPENAI_API_KEY|" in ln and "source" in ln]
        assert len(lines) == 1 and "POSTHOG" not in lines[0]
        assert "BENCHMARK_[A-Z_]+" in lines[0]

    def test_every_repo_mutation_regenerates_the_registry(self):
        for fn in ("mutate_swap", "mutate_catalog", "mutate_provider"):
            body = re.search(rf"^{fn}\(\)\{{(.*?)^\}}", self.SH, re.S | re.M).group(1)
            assert "registry_write" in body, fn

    def test_no_github_workflow_runs_the_paid_benchmark(self):
        workflows = (_ROOT / ".github" / "workflows").glob("*.yml")
        assert not [w.name for w in workflows if "model_eval_loop" in w.read_text()
                    or "benchmark_media" in w.read_text()]
