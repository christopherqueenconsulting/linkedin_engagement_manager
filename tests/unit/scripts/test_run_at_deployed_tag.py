"""Tests for scripts/run_at_deployed_tag.sh — host crons run at the DEPLOYED tag (issue #2109).

Two crons ran from a dev checkout 31 commits behind `main`, so a fix could ship and never reach the
cron that needed it. The wrapper resolves `.last_good_tag`, runs the script from a fresh worktree of
that tag and removes it. The properties that matter: the TAG's bytes run (not the working copy's,
not main's), an unreadable/unresolvable tag runs NOTHING, the worktree never outlives the run, and
the caller's cwd and exit code survive. Run for real against a temp git repo.
"""

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[3]
WRAPPER = ROOT / "scripts" / "run_at_deployed_tag.sh"
PERF_SH = ROOT / "scripts" / "perf_snapshot.sh"
SDUI_SH = ROOT / "scripts" / "weekly_sdui_drift_check.sh"
REL = "scripts/job.sh"

JOB = """#!/usr/bin/env bash
echo "{label} tag=$LEM_DEPLOYED_TAG cwd=$PWD args=$*"
exit {rc}
"""


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()


def _commit(repo: Path, label: str, rc: int = 0) -> None:
    (repo / REL).write_text(JOB.format(label=label, rc=rc), encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", label)


@pytest.fixture
def env(tmp_path: Path) -> dict:
    """An origin with v1.0.0 (deployed) then a newer main, cloned; the clone's tree is dirty."""
    origin = tmp_path / "origin"
    (origin / "scripts").mkdir(parents=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(origin)], check=True)
    _git(origin, "config", "user.email", "t@example.com")
    _git(origin, "config", "user.name", "t")
    shutil.copy(WRAPPER, origin / "scripts" / "run_at_deployed_tag.sh")
    _commit(origin, "DEPLOYED")
    _git(origin, "tag", "v1.0.0")
    _commit(origin, "MAIN")

    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", "-q", "--no-tags", str(origin), str(clone)], check=True)
    (clone / REL).write_text(JOB.format(label="WORKING-COPY", rc=0), encoding="utf-8")

    lem_root = tmp_path / "opt-lem"
    lem_root.mkdir()
    (lem_root / ".last_good_tag").write_text("v1.0.0\n", encoding="utf-8")
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    return {
        "origin": origin,
        "clone": clone,
        "lem_root": lem_root,
        "cwd": cwd,
        "log": tmp_path / "logs" / "pin.log",
        "tmp": tmp_path / "wt-tmp",
    }


def _run(env: dict, *args: str) -> subprocess.CompletedProcess:
    env["tmp"].mkdir(exist_ok=True)
    return subprocess.run(
        ["bash", str(env["clone"] / "scripts" / "run_at_deployed_tag.sh"), *args],
        cwd=env["cwd"],
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "LEM_ROOT": str(env["lem_root"]),
            "CRON_PIN_LOG": str(env["log"]),
            "TMPDIR": str(env["tmp"]),
        },
    )


def _leftover_worktrees(env: dict) -> list[str]:
    listing = _git(env["clone"], "worktree", "list", "--porcelain")
    return [line for line in listing.splitlines() if line.startswith("worktree ")][1:]


def test_runs_the_deployed_tags_bytes_not_main_or_the_working_copy(env: dict) -> None:
    """The tag had to be FETCHED (clone was --no-tags) and it is what ran."""
    proc = _run(env, REL, "a", "b")
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == f"DEPLOYED tag=v1.0.0 cwd={env['cwd']} args=a b"
    assert "run scripts/job.sh at v1.0.0" in env["log"].read_text()


def test_worktree_is_removed_after_the_run(env: dict) -> None:
    """A per-run worktree that leaks would pile up one per cron firing."""
    _run(env, REL)
    assert _leftover_worktrees(env) == []
    assert list(env["tmp"].iterdir()) == []


def test_exit_code_of_the_script_is_propagated(env: dict) -> None:
    """A failing job must still read as a failure to cron."""
    _commit(env["origin"], "FAILS", rc=3)
    _git(env["origin"], "tag", "v1.0.1")
    (env["lem_root"] / ".last_good_tag").write_text("v1.0.1", encoding="utf-8")
    proc = _run(env, REL)
    assert proc.returncode == 3
    assert "FAILS tag=v1.0.1" in proc.stdout
    assert _leftover_worktrees(env) == []


@pytest.mark.parametrize("content", [None, "", "main", "--upload-pack=x", "v1.0"])
def test_unreadable_or_malformed_tag_runs_nothing(env: dict, content: str | None) -> None:
    """Fails CLOSED: an unknown revision is exactly the defect, so nothing runs at all."""
    tag_file = env["lem_root"] / ".last_good_tag"
    if content is None:
        tag_file.unlink()
    else:
        tag_file.write_text(content, encoding="utf-8")
    proc = _run(env, REL)
    assert proc.returncode == 1
    assert proc.stdout == ""
    assert "NOT run" in env["log"].read_text()


def test_tag_that_does_not_resolve_runs_nothing(env: dict) -> None:
    """A release tag the clone cannot see (never pushed, fetch failed) is not guessed around."""
    (env["lem_root"] / ".last_good_tag").write_text("v9.9.9", encoding="utf-8")
    proc = _run(env, REL)
    assert proc.returncode == 1
    assert proc.stdout == ""
    assert "does not resolve" in env["log"].read_text()


def test_script_missing_at_the_tag_runs_nothing(env: dict) -> None:
    """A cron line added before the release that ships its script is refused, not run from main."""
    proc = _run(env, "scripts/not_there.sh")
    assert proc.returncode == 1
    assert "does not exist at v1.0.0" in env["log"].read_text()
    assert _leftover_worktrees(env) == []


def test_usage_error_without_a_script() -> None:
    """No argument is a usage error, never a no-op success."""
    proc = subprocess.run(["bash", str(WRAPPER)], capture_output=True, text=True)
    assert proc.returncode == 2


def test_stale_wrapper_warns(env: dict) -> None:
    """The wrapper cannot pin itself, so a drifted copy says so on every run."""
    wrapper = env["clone"] / "scripts" / "run_at_deployed_tag.sh"
    wrapper.write_text(wrapper.read_text() + "\n# local drift\n", encoding="utf-8")
    proc = _run(env, REL)
    assert proc.returncode == 0
    assert "cannot pin itself" in env["log"].read_text()


def _active_api_container() -> str:
    """The shipped `active_api_container` function, lifted from perf_snapshot.sh."""
    block = re.search(r"\nactive_api_container\(\)\{.*?\n\}\n", PERF_SH.read_text(), re.S)
    assert block, "active_api_container not found in perf_snapshot.sh"
    return block.group(0)


@pytest.mark.parametrize(
    ("content", "expected"),
    [("green\n", "web_api_green"), ("blue", "web_api_blue"), (None, "web_api_blue"), ("web_app", "web_api_blue")],
)
def test_margin_block_targets_the_active_api_container(tmp_path: Path, content: str | None, expected: str) -> None:
    """`web_app` is nginx with no python, so the margin block was null on 48 of 63 lines."""
    if content is not None:
        (tmp_path / ".active_color").write_text(content, encoding="utf-8")
    script = f'LEM_ROOT="{tmp_path}"\n{_active_api_container()}\nactive_api_container\n'
    proc = subprocess.run(["bash", "-c", script], capture_output=True, text=True, check=True)
    assert proc.stdout.strip() == expected


def test_perf_snapshot_never_defaults_to_web_app() -> None:
    """Guards the regression directly: the default must be the resolver, not the nginx container."""
    source = PERF_SH.read_text()
    assert 'MARGIN_CONTAINER="${MARGIN_CONTAINER:-$(active_api_container)}"' in source
    assert "MARGIN_CONTAINER:-web_app" not in source


def test_sdui_sweep_pins_the_probe_to_the_deployed_tag_when_wrapped() -> None:
    """Through the wrapper the probe matches the image it is piped into."""
    assert 'PROBE_REF="${SDUI_PROBE_REF:-${LEM_DEPLOYED_TAG:-origin/main}}"' in SDUI_SH.read_text()
