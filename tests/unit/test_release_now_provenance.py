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

pytestmark = pytest.mark.unit

WORKFLOW = (Path(__file__).resolve().parents[2] / ".github" / "workflows"
            / "release-auto-merge.yml")
SOURCE = WORKFLOW.read_text(encoding="utf-8")

# Extracted by regex rather than parsed: PyYAML is not in the unit lane's dependencies, and the
# repo already reads shell out of YAML this way (test_agent_pipeline_phasefix.py).
STEP_NAMES = re.findall(r"^      - name: (.+)$", SOURCE, re.M)


def _step(name_fragment: str) -> str:
    """The full YAML block of the step whose name contains `name_fragment`."""
    m = re.search(rf"^      - name: [^\n]*{re.escape(name_fragment)}[^\n]*\n"
                  rf"(?:.*?)(?=^      - name: |\Z)", SOURCE, re.M | re.S)
    assert m, f"step matching {name_fragment!r} not found"
    return m.group(0)


VERIFY = _step("Verify who applied")


def _run_block(step: str) -> str:
    """The step's `run:` script, dedented so bash sees it as the workflow does."""
    m = re.search(r"^        run: \|\n(.*?)(?=^        \S|\Z)", step, re.M | re.S)
    assert m, "no run: block in the verification step"
    return textwrap.dedent(m.group(1))


class TestTheGateIsWired:
    def test_the_verification_step_exists(self):
        assert VERIFY.strip()

    def test_it_runs_before_anything_enqueues_a_release(self):
        # A check that runs after the enqueue is decoration.
        assert "Verify who applied" in STEP_NAMES[0]

    def test_it_only_applies_to_the_fast_lane_not_the_scheduled_window(self):
        # The 4x-daily cron carries no PR and no label; gating it would stop every release.
        assert "if: github.event_name == 'pull_request_target'" in VERIFY

    def test_the_allowlist_is_explicit_and_not_empty(self):
        m = re.search(r'TRUSTED_LABELLERS:\s*"([^"]*)"', VERIFY)
        assert m and m.group(1).strip()

    def test_the_label_presence_check_is_still_there_too(self):
        # Provenance is an ADDITIONAL gate, not a replacement — a PR without the label must still
        # not fast-lane.
        job_if = re.search(r"^    if: >-\n(.*?)(?=^    steps:)", SOURCE, re.M | re.S).group(1)
        assert "release:now" in job_if and "merged == true" in job_if

    def test_the_step_reads_the_LAST_labeled_event(self):
        # A label removed and re-added by someone else belongs to whoever added it last.
        assert "| last" in _run_block(VERIFY)

    def test_untrusted_input_is_passed_through_env_not_interpolated_into_the_script(self):
        # The ${{ }} -> shell interpolation hazard that codeql-pr-gate.yml has. The PR number is
        # numeric, but the habit is the point.
        assert "${{" not in _run_block(VERIFY)


class TestTheGateLogic:
    """Run the step's actual shell against a stubbed `gh`."""

    @staticmethod
    def _run(tmp_path: Path, actor_output: str, trusted: str = "gitchrisqueen"):
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir(exist_ok=True)
        gh = bin_dir / "gh"
        gh.write_text("#!/usr/bin/env bash\n" + actor_output, encoding="utf-8")
        gh.chmod(0o755)
        script = _run_block(VERIFY)
        return subprocess.run(
            ["bash", "-c", script], capture_output=True, text=True,
            env={"PATH": f"{bin_dir}:/usr/bin:/bin", "HOME": str(tmp_path),
                 "GH_TOKEN": "x", "REPO": "acme/widget", "PR_NUMBER": "42",
                 "TRUSTED_LABELLERS": trusted})

    # The step pipes gh's --slurp output into jq, so the stub emits an array OF PAGES of timeline
    # events rather than a bare login.
    @staticmethod
    def _timeline(*logins: str) -> str:
        pages = ",".join(
            '[{"event":"labeled","label":{"name":"release:now"},'
            f'"actor":{{"login":"{login}"}}}}]' for login in logins)
        return f"cat <<'JSON'\n[{pages}]\nJSON\n"

    def test_a_trusted_labeller_authorises_the_fast_lane(self, tmp_path):
        r = self._run(tmp_path, self._timeline("gitchrisqueen"))
        assert r.returncode == 0
        assert "fast lane authorised" in r.stdout

    def test_anybody_else_is_refused(self, tmp_path):
        r = self._run(tmp_path, self._timeline("drive-by-contributor"))
        assert r.returncode == 1
        assert "not in TRUSTED_LABELLERS" in r.stdout

    def test_a_two_page_timeline_resolves_to_the_LAST_labeller(self, tmp_path):
        # `--paginate --jq` emitted one login per page; the comparison then always failed. With
        # --slurp the whole timeline flattens and `| last` means last overall.
        ok = self._run(tmp_path, self._timeline("someone-else", "gitchrisqueen"))
        assert ok.returncode == 0
        refused = self._run(tmp_path, self._timeline("gitchrisqueen", "someone-else"))
        assert refused.returncode == 1

    def test_an_unreadable_timeline_FAILS_CLOSED(self, tmp_path):
        # Cost of a false negative: the PR ships at the next window, hours later.
        # Cost of a false positive: an unreviewed production deploy.
        r = self._run(tmp_path, 'exit 1')
        assert r.returncode == 1
        assert "could not read" in r.stdout

    def test_an_empty_actor_fails_closed(self, tmp_path):
        r = self._run(tmp_path, "echo '[[]]'")   # a timeline with no labeled event
        assert r.returncode == 1

    def test_a_prefix_of_a_trusted_name_is_not_a_match(self, tmp_path):
        r = self._run(tmp_path, self._timeline("gitchrisqueen-evil"))
        assert r.returncode == 1

    def test_multiple_trusted_labellers_are_supported(self, tmp_path):
        r = self._run(tmp_path, self._timeline("release-bot"), trusted="gitchrisqueen release-bot")
        assert r.returncode == 0


class TestFastLaneDocsMatchTheCode:
    def test_the_doc_records_that_provenance_is_checked(self):
        doc = (Path(__file__).resolve().parents[2] / "docs" / "release-fast-lane.md"
               ).read_text(encoding="utf-8")
        assert re.search(r"provenance|who applied|TRUSTED_LABELLERS", doc, re.I), (
            "docs/release-fast-lane.md still describes release:now as label-presence only")
