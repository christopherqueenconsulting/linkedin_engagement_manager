"""Publishing a prompt-eval run (docs/prompt-evals.md §7, phase 4).

Every git and gh call goes through a fake runner, so these tests pin WHICH commands run and what the
issue bodies say — and that no generated text ever reaches GitHub.
"""

import json
import pathlib
import sys

import pytest

pytestmark = pytest.mark.unit

_ROOT = pathlib.Path(__file__).resolve().parents[3]
_SCRIPTS = _ROOT / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import prompt_eval_publish as pub  # noqa: E402

REPO = "o/r"
RUN = {"run_id": "pe-1", "date": "2026-10-12",
       "results": [{"prompt_id": "a"}, {"prompt_id": "b"}],
       "failing": [
           {"prompt_id": "comment.feed", "version": 2, "model": "openai/gpt-oss:120b",
            "kind": "prompt-fails", "reasons": ["contract (Wilson LB) 0.81 < 0.9"]},
           {"prompt_id": "comment.feed", "version": 2, "model": "openai/gpt-4o-mini",
            "kind": "fails-on-fallback", "reasons": ["judge 0.6 < 0.8"]},
           {"prompt_id": "dm.nurture", "version": 1, "model": "openai/gpt-4o-mini",
            "kind": "fails-on-fallback", "reasons": []},
       ]}


class Recorder:
    """A fake runner: returns canned stdout by command prefix and records every call."""

    def __init__(self, replies=None):
        self.replies = replies or {}
        self.calls = []

    def __call__(self, args):
        self.calls.append(list(args))
        for prefix, reply in self.replies.items():
            if " ".join(args).startswith(prefix):
                return reply
        return ""

    def ran(self, prefix):
        return [c for c in self.calls if " ".join(c).startswith(prefix)]


class TestIssues:
    def test_one_issue_per_prompt_version_covering_every_failing_model(self):
        gh = Recorder({"gh issue list": "[]"})

        done = pub.publish_issues(gh, REPO, RUN, "https://run/1")

        creates = gh.ran("gh issue create")
        assert done == ["filed comment.feed@2", "filed dm.nurture@1"]
        assert len(creates) == 2
        body = creates[0][creates[0].index("--body") + 1]
        assert body.startswith(pub.marker("comment.feed", 2))
        assert "`openai/gpt-oss:120b` | prompt-fails | contract (Wilson LB) 0.81 < 0.9" in body
        assert "`openai/gpt-4o-mini` | fails-on-fallback | judge 0.6 < 0.8" in body
        assert creates[0][creates[0].index("--title") + 1] == \
            "Prompt eval: comment.feed@2 fails its floors"
        assert creates[1][creates[1].index("--title") + 1] == \
            "Prompt eval: dm.nurture@1 fails on a fallback model"
        assert gh.ran("gh label create prompt-eval:failing")

    def test_an_open_issue_is_commented_not_duplicated(self):
        existing = [{"number": 41, "body": pub.marker("comment.feed", 2) + "\nold"},
                    {"number": 7, "body": "unrelated"}]
        gh = Recorder({"gh issue list": json.dumps(existing)})

        done = pub.publish_issues(gh, REPO, RUN, "")

        assert done == ["commented #41 (comment.feed@2)", "filed dm.nurture@1"]
        comment = gh.ran("gh issue comment 41")[0]
        assert "Still failing in run `pe-1`" in comment[comment.index("--body") + 1]

    def test_nothing_failing_files_nothing(self):
        gh = Recorder()
        assert pub.publish_issues(gh, REPO, {**RUN, "failing": []}, "") == []
        assert gh.calls == []

    def test_the_body_carries_scores_not_text(self):
        _, body = pub.render_issue("dm.nurture", 1, RUN["failing"][2:], RUN, "")
        assert "| `openai/gpt-4o-mini` | fails-on-fallback | — |" in body
        assert "Run: (local)" in body and "docs/prompt-evals/2026-10-12-pe-1.md" in body


class TestResultsPr:
    def test_no_changes_no_pr(self):
        git, gh = Recorder({"git status": ""}), Recorder()
        assert pub.publish_results_pr(git, gh, REPO, RUN, "") == "no result changes — no PR"
        assert gh.calls == [] and len(git.calls) == 1

    def test_changes_are_force_pushed_to_one_branch_and_a_pr_opened(self):
        git = Recorder({"git status": " M tests/benchmarks/prompts/eval_state.json\n?? docs/prompt-evals/x.md\n"})
        gh = Recorder({"gh pr list": "[]"})

        assert pub.publish_results_pr(git, gh, REPO, RUN, "https://run/1") == "opened PR"

        assert git.ran("git checkout -B bot/prompt-evals")
        assert git.ran("git push --force origin HEAD:refs/heads/bot/prompt-evals")
        commit = [c for c in git.calls if "commit" in c][0]
        assert "user.name=lem-prompt-evals[bot]" in commit
        create = gh.ran("gh pr create")[0]
        assert create[create.index("--head") + 1] == "bot/prompt-evals"
        assert "2 prompt × model item(s) graded, 2 prompt@version(s) failing" in \
            create[create.index("--body") + 1]

    def test_an_open_results_pr_is_updated_in_place(self):
        git = Recorder({"git status": " M docs/prompt-evals/README.md\n"})
        gh = Recorder({"gh pr list": json.dumps([{"number": 99}])})

        assert pub.publish_results_pr(git, gh, REPO, RUN, "") == "updated PR #99"
        assert not gh.ran("gh pr create")

    def test_changed_result_paths_parses_porcelain(self):
        git = Recorder({"git status": " M a/b.json\n?? docs/prompt-evals/c.md\n\n"})
        assert pub.changed_result_paths(git) == ["a/b.json", "docs/prompt-evals/c.md"]


class TestCli:
    def test_missing_results_file_is_a_no_op(self, tmp_path, capsys):
        assert pub.main(["--results", str(tmp_path / "none.json")], git=Recorder(), gh=Recorder()) == 0
        assert "nothing to publish" in capsys.readouterr().out

    def test_publishes_pr_then_issues(self, tmp_path, capsys):
        results = tmp_path / "results.json"
        results.write_text(json.dumps(RUN))
        git = Recorder({"git status": ""})
        gh = Recorder({"gh issue list": "[]"})

        assert pub.main(["--results", str(results), "--repo", REPO], git=git, gh=gh) == 0

        out = capsys.readouterr().out
        assert "no result changes" in out and "filed comment.feed@2" in out

    def test_no_pr_skips_git(self, tmp_path):
        results = tmp_path / "results.json"
        results.write_text(json.dumps({**RUN, "failing": []}))
        git = Recorder()

        assert pub.main(["--results", str(results), "--no-pr"], git=git, gh=Recorder()) == 0
        assert git.calls == []

    def test_the_real_runner_returns_stdout(self):
        assert pub.run_command(["git", "rev-parse", "--is-inside-work-tree"]).strip() == "true"
