"""The `release:now` fast lane must check WHO applied the label, not just that it is there.

`release:now` ships a PR to production the moment it merges, skipping the 4x-daily window. GitHub
labels have no ACL — anyone with triage can apply one, and `docs/release-fast-lane.md` explicitly
authorises agents to self-apply it. So presence is not evidence of authorisation, and the gate that
only checked presence was the second half of the same defect as `agent:ready` (see
docs/contribution-security.md).
"""

import re
import subprocess
import textwrap
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

WORKFLOW = (Path(__file__).resolve().parents[2] / ".github" / "workflows"
            / "release-auto-merge.yml")
SOURCE = WORKFLOW.read_text(encoding="utf-8")
PARSED = yaml.safe_load(SOURCE)
STEPS = PARSED["jobs"]["merge-pending-release"]["steps"]
VERIFY = next(s for s in STEPS if "Verify who applied" in (s.get("name") or ""))


class TestTheGateIsWired:
    def test_the_verification_step_exists(self):
        assert VERIFY is not None

    def test_it_runs_before_anything_enqueues_a_release(self):
        # A check that runs after the enqueue is decoration.
        names = [s.get("name") or "" for s in STEPS]
        assert names.index(VERIFY["name"]) == 0

    def test_it_only_applies_to_the_fast_lane_not_the_scheduled_window(self):
        # The 4x-daily cron carries no PR and no label; gating it would stop every release.
        assert VERIFY["if"] == "github.event_name == 'pull_request_target'"

    def test_the_allowlist_is_explicit_and_not_empty(self):
        assert VERIFY["env"]["TRUSTED_LABELLERS"].strip()

    def test_the_label_presence_check_is_still_there_too(self):
        # Provenance is an ADDITIONAL gate, not a replacement — a PR without the label must still
        # not fast-lane.
        cond = PARSED["jobs"]["merge-pending-release"]["if"]
        assert "release:now" in cond and "merged == true" in cond

    def test_the_step_reads_the_LAST_labeled_event(self):
        # A label removed and re-added by someone else belongs to whoever added it last.
        assert "| last" in VERIFY["run"]

    def test_untrusted_input_is_passed_through_env_not_interpolated_into_the_script(self):
        # The ${{ }} -> shell interpolation hazard that codeql-pr-gate.yml has. The PR number is
        # numeric, but the habit is the point.
        assert "${{" not in VERIFY["run"]


class TestTheGateLogic:
    """Run the step's actual shell against a stubbed `gh`."""

    @staticmethod
    def _run(tmp_path: Path, actor_output: str, trusted: str = "gitchrisqueen"):
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir(exist_ok=True)
        gh = bin_dir / "gh"
        gh.write_text("#!/usr/bin/env bash\n" + actor_output, encoding="utf-8")
        gh.chmod(0o755)
        script = textwrap.dedent(VERIFY["run"])
        return subprocess.run(
            ["bash", "-c", script], capture_output=True, text=True,
            env={"PATH": f"{bin_dir}:/usr/bin:/bin", "HOME": str(tmp_path),
                 "GH_TOKEN": "x", "REPO": "acme/widget", "PR_NUMBER": "42",
                 "TRUSTED_LABELLERS": trusted})

    def test_a_trusted_labeller_authorises_the_fast_lane(self, tmp_path):
        r = self._run(tmp_path, 'echo "gitchrisqueen"')
        assert r.returncode == 0
        assert "fast lane authorised" in r.stdout

    def test_anybody_else_is_refused(self, tmp_path):
        r = self._run(tmp_path, 'echo "drive-by-contributor"')
        assert r.returncode == 1
        assert "not in TRUSTED_LABELLERS" in r.stdout

    def test_an_unreadable_timeline_FAILS_CLOSED(self, tmp_path):
        # Cost of a false negative: the PR ships at the next window, hours later.
        # Cost of a false positive: an unreviewed production deploy.
        r = self._run(tmp_path, 'exit 1')
        assert r.returncode == 1
        assert "could not read" in r.stdout

    def test_an_empty_actor_fails_closed(self, tmp_path):
        r = self._run(tmp_path, 'echo ""')
        assert r.returncode == 1

    def test_a_prefix_of_a_trusted_name_is_not_a_match(self, tmp_path):
        r = self._run(tmp_path, 'echo "gitchrisqueen-evil"')
        assert r.returncode == 1

    def test_multiple_trusted_labellers_are_supported(self, tmp_path):
        r = self._run(tmp_path, 'echo "release-bot"', trusted="gitchrisqueen release-bot")
        assert r.returncode == 0


class TestFastLaneDocsMatchTheCode:
    def test_the_doc_records_that_provenance_is_checked(self):
        doc = (Path(__file__).resolve().parents[2] / "docs" / "release-fast-lane.md"
               ).read_text(encoding="utf-8")
        assert re.search(r"provenance|who applied|TRUSTED_LABELLERS", doc, re.I), (
            "docs/release-fast-lane.md still describes release:now as label-presence only")
