"""Publishing a prompt-eval run (docs/prompt-evals.md §7, phase 4).

Every git and gh call goes through a fake runner, so these tests pin WHICH commands run and what the
issue bodies say — and that no generated text ever reaches GitHub.
"""

import json
import pathlib
import sys
from collections.abc import Sequence

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

    def __init__(self, replies: dict[str, str] | None = None) -> None:
        self.replies = replies or {}
        self.calls = []

    def __call__(self, args: Sequence[str]) -> str:
        self.calls.append(list(args))
        for prefix, reply in self.replies.items():
            if " ".join(args).startswith(prefix):
                return reply
        return ""

    def ran(self, prefix: str) -> list[list[str]]:
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

    def test_existing_issues_are_found_by_title_not_by_a_label_triage_may_strip(self):
        gh = Recorder({"gh issue list": "[]"})
        pub.open_failure_issues(gh, REPO)
        listing = gh.ran("gh issue list")[0]
        assert "--label" not in listing
        assert listing[listing.index("--search") + 1] == 'in:title "Prompt eval:"'

    def test_nothing_failing_files_nothing(self):
        gh = Recorder()
        assert pub.publish_issues(gh, REPO, {**RUN, "failing": []}, "") == []
        assert gh.calls == []

    def test_a_broken_marker_is_ignored_not_a_crash(self):
        existing = [{"number": 5, "body": "<!-- prompt-eval:comment.feed@2 never closed"}]
        gh = Recorder({"gh issue list": json.dumps(existing)})

        assert pub.open_failure_issues(gh, REPO) == {}

    def test_a_pipe_in_a_reason_cannot_break_the_table(self):
        failure = {**RUN["failing"][0], "reasons": ["a | b"]}
        _, body = pub.render_issue("comment.feed", 2, [failure], RUN, "")
        assert "| prompt-fails | a \\| b |" in body

    def test_mixed_version_types_still_sort(self):
        run = {**RUN, "failing": [{**RUN["failing"][2], "version": "1a"}, RUN["failing"][2]]}
        assert pub.publish_issues(Recorder({"gh issue list": "[]"}), REPO, run, "") == \
            ["filed dm.nurture@1", "filed dm.nurture@1a"]

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
        edit = gh.ran("gh pr edit 99")[0]  # the force-pushed run renames the open PR (squash subject)
        assert edit[edit.index("--title") + 1] == f"chore(prompt-evals): results of run {RUN['run_id']}"
        assert RUN["run_id"] in edit[edit.index("--body") + 1]

    def test_the_doc_index_is_committed_with_the_results(self):
        """Without docs/README.md the run's new report is unlinked and CM013 fails the bot PR."""
        assert "docs/README.md" in pub.RESULT_PATHS
        git = Recorder({"git status": " M docs/README.md\n"})
        pub.publish_results_pr(git, Recorder({"gh pr list": "[]"}), REPO, RUN, "")
        assert "docs/README.md" in git.ran("git add")[0]

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

    def test_a_malformed_results_file_fails_loudly(self, tmp_path, capsys):
        results = tmp_path / "results.json"
        results.write_text(json.dumps({"failing": []}))
        gh = Recorder()

        assert pub.main(["--results", str(results)], git=Recorder(), gh=gh) == 1
        assert "missing run_id, date" in capsys.readouterr().err
        assert gh.calls == []

    def test_corrupt_json_fails_loudly(self, tmp_path, capsys):
        results = tmp_path / "results.json"
        results.write_text("{not json")

        assert pub.main(["--results", str(results)], git=Recorder(), gh=Recorder()) == 1
        assert "is not valid JSON" in capsys.readouterr().err

    def test_no_pr_skips_git(self, tmp_path):
        results = tmp_path / "results.json"
        results.write_text(json.dumps({**RUN, "failing": []}))
        git = Recorder()

        assert pub.main(["--results", str(results), "--no-pr"], git=git, gh=Recorder()) == 0
        assert git.calls == []

    def test_the_real_runner_returns_stdout(self):
        assert pub.run_command([sys.executable, "-c", "print('ok')"]).strip() == "ok"


def _from_name(text: str) -> str:
    """The workflow from its `name:` line on — the header comments may differ between the copies."""
    return text[text.index("\nname: ") + 1:]


class TestWorkflowCopy:
    DOCS = _ROOT / "docs/prompt-evals/prompt-evals.workflow.yml"
    LIVE = _ROOT / ".github/workflows/prompt-evals.yml"

    def test_the_live_workflow_matches_the_reviewed_copy(self):
        if not self.LIVE.exists():
            pytest.skip("the owner has not committed .github/workflows/prompt-evals.yml yet")
        assert _from_name(self.LIVE.read_text()) == _from_name(self.DOCS.read_text()), (
            "edit docs/prompt-evals/prompt-evals.workflow.yml and .github/workflows/prompt-evals.yml "
            "together")

    def test_the_push_credential_is_the_pat(self):
        text = self.DOCS.read_text()
        assert "token: ${{ secrets.RELEASE_DISPATCH_TOKEN || github.token }}" in text
