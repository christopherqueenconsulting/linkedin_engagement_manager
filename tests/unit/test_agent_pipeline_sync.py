"""The pipeline's self-updater must fail boringly (#1398).

The pipeline is not in the Docker image and no workflow deploys it, so `main` and the running
pipeline could diverge silently — and did: the webhook receiver ran 23-hour-old code through nine
merged changes before anyone noticed (#1412).

An updater for the thing that runs agents with the owner's credentials is only worth having if its
failure modes are dull. So the tests here are mostly about refusing and rolling back, not about the
happy path: no change must do nothing, a box-edited file must stop everything, and a daemon that
does not come back must be put back the way it was.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

_PIPELINE = Path(__file__).resolve().parents[2] / "scripts" / "agent-pipeline"
SYNC = _PIPELINE / "sync.sh"


@pytest.fixture
def box(tmp_path: Path):
    """A scratch BASE, a scratch git mirror, and stub `systemctl`/`sudo` on PATH."""
    base = tmp_path / "base"
    src = tmp_path / "src"
    bin_ = tmp_path / "bin"
    for d in (base / "state", base / "logs", base / "lib", base / "v2" / "systemd", bin_):
        d.mkdir(parents=True)
    (base / "v2" / "marker.py").write_text("v = 1\n")
    (base / "lib" / "posthog.sh").write_text("posthog_capture() { :; }\n")

    # A real git repo: the ancestor check is a real `git merge-base`, not something to fake.
    pipeline = src / "scripts" / "agent-pipeline"
    pipeline.mkdir(parents=True)
    (pipeline / "install.sh").write_text("#!/usr/bin/env bash\nexit 0\n")
    (pipeline / "install.sh").chmod(0o755)
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    for cmd in (["git", "init", "-q", "-b", "main"], ["git", "add", "-A"],
                ["git", "commit", "-qm", "init"]):
        subprocess.run(cmd, cwd=src, env=env, check=True, capture_output=True)
    # ONLY the remote-tracking ref, never a local branch called `origin/main`. Creating both makes
    # `git rev-parse origin/main` ambiguous — it resolves the local branch first — and the ancestor
    # check then compares against a ref the test never advances.
    subprocess.run(["git", "update-ref", "refs/remotes/origin/main", "HEAD"],
                   cwd=src, env=env, check=True, capture_output=True)

    calls = tmp_path / "calls.txt"
    for name in ("systemctl", "sudo"):
        p = bin_ / name
        p.write_text(f'#!/usr/bin/env bash\necho "{name} $*" >> {calls}\nexit 0\n')
        p.chmod(0o755)
    return base, src, bin_, calls, env


def _run(box, **extra):
    """Run sync.sh against the scratch box."""
    base, src, bin_, _, _ = box
    env = {
        "PATH": f"{bin_}:/usr/bin:/bin", "BASE": str(base), "HOME": str(base),
        "LEM_SYNC_SRC": str(src), "LEM_SYNC_VERIFY_SECONDS": "6", **extra,
    }
    return subprocess.run(["bash", str(SYNC)], capture_output=True, text=True, timeout=90, env=env)


def _beat(base: Path, value: int) -> None:
    """Set the heartbeat the verify loop watches."""
    (base / "state" / "lemd.heartbeat").write_text(str(value))


# ---------------------------------------------------------------- refusals


def test_the_kill_switch_stops_the_updater(box):
    """The switch has to be outside the thing it kills, or a bad sync removes the means to stop it."""
    base = box[0]
    (base / "state" / "SYNC_HOLD").touch()
    got = _run(box)
    assert got.returncode == 0
    assert "SYNC_HOLD present" in got.stdout
    assert not box[3].exists(), "nothing should have been restarted"


def test_a_commit_that_is_not_on_main_is_refused(box):
    """Only `main` is ever deployed, and only a commit that is an ancestor of it."""
    base, src, _, _, env = box
    subprocess.run(["git", "checkout", "-qb", "side"], cwd=src, env=env, check=True,
                   capture_output=True)
    (src / "scripts" / "agent-pipeline" / "rogue.sh").write_text("echo hi\n")
    subprocess.run(["git", "add", "-A"], cwd=src, env=env, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-qm", "side"], cwd=src, env=env, check=True,
                   capture_output=True)
    got = _run(box)
    assert got.returncode == 1
    assert "not an ancestor of origin/main" in got.stdout


def test_a_missing_mirror_is_fatal_not_silent(box):
    """A sync that cannot find its source must say so rather than reporting success."""
    got = _run(box, LEM_SYNC_SRC=str(box[0] / "nope"))
    assert got.returncode == 1
    assert "no git checkout" in got.stdout


# ---------------------------------------------------------------- the no-op case


def test_an_unchanged_tree_does_nothing_and_says_nothing(box):
    """This runs several times an hour. It must be silent and must not restart anything."""
    base = box[0]
    _beat(base, 100)
    first = _run(box)
    assert first.returncode == 0
    box[3].unlink(missing_ok=True)

    second = _run(box)
    assert second.returncode == 0
    assert second.stdout.strip() == "", "a no-op sync should be quiet"
    assert not box[3].exists(), "a no-op sync must not restart anything"


# ---------------------------------------------------------------- the failure paths


def test_a_box_edited_file_stops_everything(box):
    """`install.sh --sync` refuses rather than overwriting a file someone is mid-debug on.

    That refusal is a safety property, so the updater must treat it as a stop — not retry it with
    `--force`, and above all not restart the daemon as though the sync had worked.
    """
    base, src, _, calls, _ = box
    (src / "scripts" / "agent-pipeline" / "install.sh").write_text(
        "#!/usr/bin/env bash\necho 'REFUSED (box-local edits)'\nexit 1\n")
    (src / "scripts" / "agent-pipeline" / "install.sh").chmod(0o755)
    got = _run(box)
    assert got.returncode == 1
    assert "REFUSED" in got.stdout
    assert not calls.exists(), "nothing may be restarted after a refused install"


def test_a_daemon_that_does_not_come_back_is_rolled_back(box):
    """Liveness AND freshness — a unit that is `active` with a frozen heartbeat is the failure.

    The stub `systemctl` always reports success, so this exercises exactly the case a naive check
    would miss: the restart "worked" and the daemon is not actually running.
    """
    base, src, _, calls, env = box
    _beat(base, 100)                      # never advances
    pipeline = src / "scripts" / "agent-pipeline"
    (pipeline / "install.sh").write_text(
        f"#!/usr/bin/env bash\necho 'v = 2' > {base}/v2/marker.py\nexit 0\n")
    (pipeline / "install.sh").chmod(0o755)
    subprocess.run(["git", "add", "-A"], cwd=src, env=env, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-qm", "change"], cwd=src, env=env, check=True,
                   capture_output=True)
    subprocess.run(["git", "update-ref", "refs/remotes/origin/main", "HEAD"],
                   cwd=src, env=env, check=True, capture_output=True)

    got = _run(box)
    assert got.returncode == 1
    assert "ROLLING BACK" in got.stdout
    assert (base / "v2" / "marker.py").read_text().strip() == "v = 1", "the snapshot was not restored"
    assert not (base / "state" / "synced.sha").exists(), (
        "a rolled-back sync must not record success, or the next run skips the retry"
    )


def test_both_units_are_restarted(box):
    """Restarting only the daemon is exactly how the receiver ended up 23 hours stale (#1412).

    Both load the `lemd` package, so both must come back.
    """
    base, src, _, calls, env = box
    pipeline = src / "scripts" / "agent-pipeline"
    (pipeline / "install.sh").write_text(
        f"#!/usr/bin/env bash\necho 'v = 2' > {base}/v2/marker.py\nexit 0\n")
    (pipeline / "install.sh").chmod(0o755)
    subprocess.run(["git", "add", "-A"], cwd=src, env=env, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-qm", "change"], cwd=src, env=env, check=True,
                   capture_output=True)
    subprocess.run(["git", "update-ref", "refs/remotes/origin/main", "HEAD"],
                   cwd=src, env=env, check=True, capture_output=True)
    _beat(base, 1)
    _run(box, LEM_SYNC_VERIFY_SECONDS="1")

    restarts = calls.read_text()
    assert "lem-agentd.service" in restarts
    assert "lem-agent-webhook.service" in restarts


# ---------------------------------------------------------------- shipping and safety


def test_the_updater_ships_itself():
    """A box keeps whatever updater it was first installed with, otherwise."""
    installer = (_PIPELINE / "install.sh").read_text()
    assert "sync.sh" in installer
    assert 'for f in "$SRC"/systemd/*' in installer, "top-level systemd/ still is not shipped"


def test_the_units_exist_and_pull_before_they_exec():
    """The fetch must happen BEFORE sync.sh is exec'd, or the script is rewritten while bash reads it."""
    unit = (_PIPELINE / "systemd" / "lem-pipeline-sync.service").read_text()
    assert "ExecStartPre=/usr/bin/git" in unit
    assert unit.index("ExecStartPre") < unit.index("ExecStart=")
    assert "User=lem" in unit
    timer = (_PIPELINE / "systemd" / "lem-pipeline-sync.timer").read_text()
    assert "OnUnitActiveSec=" in timer


def test_the_sync_never_forces_the_install():
    """`--force` would overwrite a box-edited file, which is the one thing --sync exists to refuse."""
    body = SYNC.read_text()
    # The INVOCATION line, not the header comment that explains why --force is not used — matching
    # prose is how a test ends up forbidding its own documentation.
    call = next(ln for ln in body.splitlines()
                if "install.sh" in ln and not ln.lstrip().startswith("#"))
    assert "--sync" in call and "--force" not in call, call


def test_the_mirror_is_not_the_human_workspace():
    """Shipping whatever a half-finished rebase left in the dev checkout is a live hazard."""
    body = SYNC.read_text()
    assert "/home/lem/agent-pipeline-src" in body
    assert "linkedin_engagement_manager" not in body.split("LEM_SYNC_SRC")[1][:200]
