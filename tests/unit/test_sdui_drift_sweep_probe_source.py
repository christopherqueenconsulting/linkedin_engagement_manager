"""Tests for `pin_script` in scripts/weekly_sdui_drift_check.sh.

`scripts/` is not baked into the image, so the weekly sweep reads both the probe and the filer off
disk from `$REPO` and pipes them in — and `$REPO` is an ordinary git checkout nobody pulls. On
2026-09-21 it sat one commit behind, re-ran the pre-#2069 company-invite probe, graded the surface
`unknown` for a fourth week and re-filed the blind-spot issue that fix had already closed (#2085).

`pin_script` resolves each script from a ref instead, so the properties that matter are: it hands
back the REF's bytes rather than the working copy's, it never fails the sweep when the ref cannot
be read, and the wiring downstream actually pipes what it returned. The function is lifted verbatim
from the shipped file and run for real against a temp git repo, following the harness in
test_agent_pipeline_graft_seed.py.
"""

import re
import subprocess
import textwrap
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

SWEEP_SH = Path(__file__).resolve().parents[2] / "scripts" / "weekly_sdui_drift_check.sh"
SOURCE = SWEEP_SH.read_text(encoding="utf-8")
REL = "scripts/linkedin_live_validation.py"


def _pin_script() -> str:
    """The shipped `pin_script` function body, lifted from the sweep script."""
    block = re.search(r"\npin_script\(\)\{.*?\n\}\n", SOURCE, re.S)
    assert block, "pin_script not found in weekly_sdui_drift_check.sh"
    return block.group(0)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A checkout whose `main` holds the FIXED probe while the working copy is still stale."""
    root = tmp_path / "clone"
    (root / "scripts").mkdir(parents=True)
    git = ["git", "-C", str(root)]
    subprocess.run(["git", "init", "-q", "-b", "main", str(root)], check=True)
    subprocess.run(git + ["config", "user.email", "t@example.com"], check=True)
    subprocess.run(git + ["config", "user.name", "t"], check=True)
    (root / REL).write_text("STALE probe\n", encoding="utf-8")
    subprocess.run(git + ["add", "-A"], check=True)
    subprocess.run(git + ["commit", "-qm", "stale"], check=True)
    # The ref moves on; the working tree is left on the old bytes, exactly as the box was found.
    (root / REL).write_text("FIXED probe\n", encoding="utf-8")
    subprocess.run(git + ["add", "-A"], check=True)
    subprocess.run(git + ["commit", "-qm", "fixed"], check=True)
    subprocess.run(git + ["checkout", "-q", "HEAD~1", "--", REL], check=True)
    return root


def _run(repo: Path, tmp_path: Path, rel: str = REL, ref: str = "main") -> subprocess.CompletedProcess:
    log = tmp_path / "sweep.log"
    pinned_dir = tmp_path / "pinned"
    pinned_dir.mkdir(exist_ok=True)
    # The shipped script `rm -rf`s this on EXIT; here it outlives the shell so the bytes it handed
    # back can be read.
    script = textwrap.dedent(
        f"""
        set -uo pipefail
        REPO="{repo}"
        LOG="{log}"
        PROBE_REF="{ref}"
        PINNED_DIR="{pinned_dir}"
        log(){{ echo "[log] $*" >>"$LOG"; }}
        {_pin_script()}
        pin_script "{rel}"
        """
    )
    result = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
    result.stderr = (log.read_text(encoding="utf-8") if log.exists() else "") + result.stderr
    return result


def test_pins_the_ref_not_the_working_copy(repo: Path, tmp_path: Path) -> None:
    """The whole defect: the working copy is stale, so the ref's bytes are what must be swept."""
    result = _run(repo, tmp_path)
    assert result.returncode == 0
    pinned = Path(result.stdout.strip())
    assert pinned.read_text(encoding="utf-8") == "FIXED probe\n"
    assert (repo / REL).read_text(encoding="utf-8") == "STALE probe\n"
    assert str(repo) not in str(pinned), "the pinned copy must not be a path inside the checkout"


def test_unreadable_ref_falls_open_to_the_working_copy(repo: Path, tmp_path: Path) -> None:
    """A sweep on a stale probe still measures more than no sweep at all — but it says so."""
    result = _run(repo, tmp_path, ref="origin/does-not-exist")
    assert result.returncode == 0
    assert result.stdout.strip() == f"{repo}/{REL}"
    assert "WARNING" in result.stderr


def test_missing_path_in_the_ref_falls_open(repo: Path, tmp_path: Path) -> None:
    """A path the ref never carried is the same fail-open, not an empty file piped into python."""
    result = _run(repo, tmp_path, rel="scripts/never_existed.py")
    assert result.returncode == 0
    assert result.stdout.strip() == f"{repo}/scripts/never_existed.py"
    assert "WARNING" in result.stderr


def test_sweep_pipes_the_pinned_copies() -> None:
    """The wiring half: pinning is worthless if the invocations still read `$REPO/scripts/...`."""
    assert '< "$PROBE_SCRIPT"' in SOURCE
    assert '"$FILER_SCRIPT"' in SOURCE
    assert 'PROBE_SCRIPT="$(pin_script scripts/linkedin_live_validation.py)"' in SOURCE
    assert 'FILER_SCRIPT="$(pin_script scripts/sdui_drift_issues.py)"' in SOURCE
    assert "$REPO/scripts/linkedin_live_validation.py" not in SOURCE.split("pin_script(){")[0]


def test_ref_is_refreshed_before_it_is_pinned() -> None:
    """`origin/main` is exactly as stale as HEAD until something fetches it."""
    fetch = re.search(r"^git -C \"\$REPO\" fetch .*$", SOURCE, re.M)
    assert fetch, "the sweep must fetch before pinning"
    assert SOURCE.index(fetch.group(0)) < SOURCE.index('PROBE_SCRIPT="$(pin_script')
    # A fetch failure is not a broken sweep — `pin_script` still resolves the on-disk ref.
    assert "|| log" in SOURCE[SOURCE.index(fetch.group(0)):SOURCE.index(fetch.group(0)) + 300]


def test_fail_open_pin_pages_instead_of_only_logging() -> None:
    """A fail-open pin re-creates the defect, so it must alert — a log WARNING is what hid it."""
    fallback_alert = re.search(
        r'if \[ "\$PROBE_SCRIPT" = "\$REPO/scripts/linkedin_live_validation\.py" \].*?\nfi\n',
        SOURCE, re.S)
    assert fallback_alert, "a fail-open pin must be detected after pinning"
    assert '"$FILER_SCRIPT" = "$REPO/scripts/sdui_drift_issues.py"' in fallback_alert.group(0)
    assert "alert " in fallback_alert.group(0), "the fail-open path must page, not just log"
    # It has to run after both pins and before the probe is piped in, or it reports nothing.
    assert SOURCE.index('FILER_SCRIPT="$(pin_script') < SOURCE.index(fallback_alert.group(0))
    assert SOURCE.index(fallback_alert.group(0)) < SOURCE.index('< "$PROBE_SCRIPT"')


def test_sweep_logs_which_revision_measured() -> None:
    """A stale sweep has to be legible after the fact, not read as a healthy one."""
    assert "probe source: $PROBE_REF" in SOURCE
    assert "checkout HEAD" in SOURCE
