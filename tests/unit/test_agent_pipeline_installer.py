"""Regression tests for scripts/agent-pipeline/install.sh.

The installer runs against a LIVE pipeline box, so its failures are not crashes — they are silent
losses. Two have already happened: an unconditional `touch PAUSED` stopped the running pipeline on
a re-install, and a hand-edited `lib/run_lane.sh` on the box existed nowhere in git and would have
been destroyed by the obvious way of syncing `lib/`. `--sync` exists to refuse exactly that file,
and the refusal is only worth anything if it holds on EVERY later sync — recording a refused file
in the manifest would make the next run read the box edit as ours and overwrite it without asking.

Everything here runs the shipped script for real against a scratch $LEM_PIPELINE_DEST, with `SRC`
a throwaway copy of the repo tree so a test may edit "the repo" side, and a stub `crontab` on PATH
so the box's real crontab is never touched.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

PIPELINE_DIR = Path(__file__).resolve().parents[2] / "scripts" / "agent-pipeline"

pytestmark = [
    pytest.mark.unit,
    pytest.mark.skipif(shutil.which("bash") is None, reason="install.sh needs bash"),
]


@pytest.fixture
def box(tmp_path):
    """A scratch (src, dest, env) triple — a copy of the repo tree and an empty box."""
    src = tmp_path / "src"
    shutil.copytree(PIPELINE_DIR, src)
    dest = tmp_path / "dest"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    # `crontab -` must DRAIN stdin like the real one: a stub that exits first SIGPIPEs the writer,
    # and `set -o pipefail` turns that into a 141 exit for the installer.
    (bin_dir / "crontab").write_text(
        '#!/bin/sh\n[ "$1" = "-" ] && cat >/dev/null\nexit 0\n', encoding="utf-8"
    )
    (bin_dir / "crontab").chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "LEM_PIPELINE_DEST": str(dest),
    }
    return src, dest, env


def _run(box, *args):
    src, _, env = box
    return subprocess.run(
        ["bash", str(src / "install.sh"), *args],
        capture_output=True, text=True, timeout=120, env=env,
        stdin=subprocess.DEVNULL,
    )


def test_first_install_pauses_and_re_install_does_not(box):
    """A re-install on a live box must never pause it — that outage happened on 2026-08-09."""
    _, dest, _ = box
    assert _run(box).returncode == 0
    assert (dest / "PAUSED").exists()
    assert (dest / "tick.sh").exists() and os.access(dest / "tick.sh", os.X_OK)
    assert (dest / "lib" / "run_lane.sh").exists()
    assert list((dest / "docs").glob("*.md")), "docs/ must reach the box, it used to drift"

    (dest / "PAUSED").unlink()
    assert _run(box).returncode == 0
    assert not (dest / "PAUSED").exists()


def test_sync_refuses_a_box_edited_file_on_every_run(box):
    """The refusal must not decay.

    Writing a refused file's CURRENT hash to the manifest would make the next `--sync` read the box
    edit as "unchanged since we placed it" and overwrite it silently — a one-run guard that loses
    the file on run two is worse than no guard, because the operator was told it was protected.
    """
    src, dest, _ = box
    assert _run(box).returncode == 0

    edited = dest / "lib" / "run_lane.sh"
    edited.write_text(edited.read_text(encoding="utf-8") + "\n# box-local hotfix\n", encoding="utf-8")
    clean = src / "tick.sh"
    clean.write_text(clean.read_text(encoding="utf-8") + "\n# repo moved on\n", encoding="utf-8")

    for run in range(3):
        proc = _run(box, "--sync")
        assert proc.returncode == 1, f"run {run}: {proc.stdout}{proc.stderr}"
        assert "lib/run_lane.sh" in proc.stdout
        assert "# box-local hotfix" in edited.read_text(encoding="utf-8"), f"clobbered on run {run}"
    # The clean file alongside it still got its update on the first pass.
    assert "# repo moved on" in (dest / "tick.sh").read_text(encoding="utf-8")


def test_force_overwrites_and_then_sync_is_clean(box):
    """After --force the box copy is ours again, so the next sync must stop refusing it."""
    src, dest, _ = box
    assert _run(box).returncode == 0
    edited = dest / "lib" / "run_lane.sh"
    edited.write_text(edited.read_text(encoding="utf-8") + "\n# box-local hotfix\n", encoding="utf-8")

    forced = _run(box, "--sync", "--force")
    assert forced.returncode == 0, forced.stdout + forced.stderr
    assert "# box-local hotfix" not in edited.read_text(encoding="utf-8")

    after = _run(box, "--sync")
    assert after.returncode == 0, after.stdout + after.stderr
    assert "0 refused" in after.stdout


def test_sync_without_an_install_is_refused(box):
    """`--sync` onto an empty box would place files and skip PAUSED — say so instead."""
    proc = _run(box, "--sync")
    assert proc.returncode == 2
    assert "holds no install" in proc.stderr


def test_help_prints_the_whole_header(box):
    """A fixed line range truncated the usage text mid-sentence when the header grew."""
    proc = _run(box, "--help")
    assert proc.returncode == 0
    assert "--sync --force" in proc.stdout
    assert "set -euo pipefail" not in proc.stdout


def test_scratch_install_never_touches_the_crontab(tmp_path, monkeypatch):
    """LEM_PIPELINE_DEST exists so this installer can be tested; it must not schedule anything.

    The previous version added a cron line for whatever path it was pointed at, so exercising it
    left `*/5 * * * * /tmp/tmp.XXXX/dest/tick.sh` behind. Two of those accumulated on the live box
    and fired every five minutes against deleted paths until an audit noticed. A test harness that
    can schedule work on the host is a bug regardless of whether the work does anything.
    """
    src = Path(__file__).resolve().parents[2] / "scripts" / "agent-pipeline"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    # A crontab stub that records any write; the real one must never be reached.
    (fake_bin / "crontab").write_text(
        "#!/usr/bin/env bash\n"
        f'if [ "$1" = "-l" ]; then cat "{tmp_path}/cron.txt" 2>/dev/null; exit 0; fi\n'
        f'cat > "{tmp_path}/cron_written.txt"\n'
    )
    (fake_bin / "crontab").chmod(0o755)
    (tmp_path / "cron.txt").write_text("")

    env = dict(os.environ)
    env["PATH"] = f"{fake_bin}:{env['PATH']}"
    env["LEM_PIPELINE_DEST"] = str(tmp_path / "dest")
    subprocess.run([str(src / "install.sh")], cwd=src, env=env, capture_output=True, text=True,
                   check=False, timeout=60)

    assert not (tmp_path / "cron_written.txt").exists(), "a scratch install wrote to the crontab"


def test_cron_line_targets_the_install_destination():
    """The scheduled path and the installed path must be the same one."""
    text = (Path(__file__).resolve().parents[2] / "scripts" / "agent-pipeline"
            / "install.sh").read_text()
    assert 'LINE="*/5 * * * * $DEST/tick.sh' in text
