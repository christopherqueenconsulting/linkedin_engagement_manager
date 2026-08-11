"""A run the daemon ended must not cost the item a budget charge (#1391).

The ledger charges at DISPATCH and nothing ever decremented it. So a run the daemon itself killed or
adopted-and-closed still consumed one of the item's attempts — and there were 16 daemon restarts in
one night on 2026-08-10, each taxing the whole in-flight backlog for runs it never got.

The rule is asymmetric on purpose, and the asymmetry is the whole design:

* `RC_VANISHED` — an orphan or crash recovery. **We** ended it; it measured nothing about whether
  the work converges. Refund.
* `RC_KILLED` — a deadline kill. A run that consumed its entire wall-clock ceiling and produced
  nothing is precisely what a budget exists to stop repeating. Refunding it is an unbounded loop.

The remaining risk — a process that dies early every time — is capped at one refund per (item, mode).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_PIPELINE = _ROOT / "scripts" / "agent-pipeline"
_V2 = _PIPELINE / "v2"
sys.path.insert(0, str(_V2))

from lemd import db, policy  # noqa: E402


def _ledger(tmp_path: Path):
    """A bash shim that runs one ledger function against a scratch BASE."""
    def run(fn: str, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["bash", "-c",
             f'BASE="{tmp_path}"; . "{_PIPELINE}/lib/ledger.sh"; {fn} ' + " ".join(args)],
            capture_output=True, text=True, timeout=20,
        )
    return run


# ---------------------------------------------------------------- the rule


def test_a_vanished_run_is_refundable():
    """We ended it, so it proves nothing about the work."""
    assert policy.refundable(db.RC_VANISHED) is True


def test_a_deadline_kill_is_not_refundable():
    """A run that used its whole ceiling and produced nothing is what a budget is FOR.

    If deadline kills ever dominate a lane, the answer is a bigger budget for that lane — not a
    refund, which is unbounded.
    """
    assert policy.refundable(db.RC_KILLED) is False


@pytest.mark.parametrize("rc", [0, 1, 70, 71, 72, 73, None])
def test_nothing_else_is_refundable(rc):
    """Success, failure and every refusal all mean the run happened."""
    assert policy.refundable(rc) is False


# ---------------------------------------------------------------- the ledger write


def test_a_refund_decrements_the_count(tmp_path):
    """The charge comes back, read through the SAME parser the daemon uses."""
    run = _ledger(tmp_path)
    run("ledger_charge", "pr", "1", "start")
    run("ledger_charge", "pr", "1", "start")
    assert policy.ledger_count(tmp_path, "pr", 1, "start") == 2
    assert run("ledger_refund", "pr", "1", "start").returncode == 0
    assert policy.ledger_count(tmp_path, "pr", 1, "start") == 1


def test_only_one_refund_per_item_and_mode(tmp_path):
    """The cap that defuses a process which dies early every time.

    It gets exactly one extra attempt, then the budget converges as before.
    """
    run = _ledger(tmp_path)
    for _ in range(3):
        run("ledger_charge", "pr", "1", "start")
    assert run("ledger_refund", "pr", "1", "start").returncode == 0
    assert run("ledger_refund", "pr", "1", "start").returncode == 1
    assert policy.ledger_count(tmp_path, "pr", 1, "start") == 2


def test_a_refund_never_goes_below_zero(tmp_path):
    """Nothing charged means nothing to give back."""
    run = _ledger(tmp_path)
    run("ledger_charge", "pr", "1", "start")
    assert run("ledger_refund", "pr", "1", "start").returncode == 0
    assert run("ledger_refund", "pr", "1", "start").returncode == 1
    assert policy.ledger_count(tmp_path, "pr", 1, "start") == 0


def test_a_refund_on_an_absent_item_is_refused_not_fatal(tmp_path):
    """The daemon calls this best-effort on every adopted orphan."""
    assert _ledger(tmp_path)("ledger_refund", "pr", "9", "start").returncode == 1


def test_a_refund_leaves_other_modes_alone(tmp_path):
    """One row per mode; a refund must not disturb its neighbours."""
    run = _ledger(tmp_path)
    run("ledger_charge", "pr", "1", "start")
    run("ledger_charge", "pr", "1", "fix")
    run("ledger_refund", "pr", "1", "start")
    assert policy.ledger_count(tmp_path, "pr", 1, "fix") == 1


# ---------------------------------------------------------------- format parity


def test_the_fifth_field_is_readable_by_both_parsers(tmp_path):
    """The ledger is shared byte-for-byte with v1, so a new field must not break either reader.

    Asserted rather than assumed: `lib/ledger.sh` reads fields 2 and 4 with `cut`, and
    `policy.ledger_count` slices `parts[0..3]` — both tolerate a 5th, and this proves it on a row
    that actually has one.
    """
    run = _ledger(tmp_path)
    run("ledger_charge", "pr", "1", "start")
    run("ledger_charge", "pr", "1", "start")
    run("ledger_refund", "pr", "1", "start")

    raw = (tmp_path / "state" / "ledger" / "pr-1.tsv").read_text().strip()
    assert len(raw.split("\t")) == 5, raw

    # The bash reader.
    got = run("ledger_count", "pr", "1", "start")
    assert got.stdout.strip() == "1"
    # ...and the Python one.
    assert policy.ledger_count(tmp_path, "pr", 1, "start") == 1


def test_a_rotated_reset_key_refuses_the_refund(tmp_path):
    """A rotated key already reset the count; refunding against it would go negative in meaning."""
    run = _ledger(tmp_path)
    run("ledger_charge", "pr", "1", "merge", "sha-a")
    assert run("ledger_refund", "pr", "1", "merge", "sha-b").returncode == 1


# ---------------------------------------------------------------- the wiring


def test_the_daemon_refunds_an_adopted_orphan():
    """`_reap_adopted` is where a vanished run is recognised, so it is where the charge comes back."""
    src = (_V2 / "lemd" / "dispatch.py").read_text()
    reap = src[src.index("def _reap_adopted"):src.index("def _refund")]
    assert "db.RC_VANISHED" in reap
    assert "self._refund(row)" in reap


def test_the_refund_needs_the_runs_mode():
    """A refund is per (item, MODE); selecting without it would refund the wrong lane."""
    src = (_V2 / "lemd" / "dispatch.py").read_text()
    assert "SELECT id, item_id, mode, pid, pid_start FROM runs" in src


def test_bash_remains_the_only_ledger_writer():
    """v1 and v2 share the ledger byte-for-byte; a second writer breaks rollback in both directions.

    So the refund is a tiny script rather than a Python file write, and no `.tsv` is opened for
    writing anywhere in `lemd/`.
    """
    assert (_V2 / "actions" / "refund.sh").exists()
    for py in (_V2 / "lemd").glob("*.py"):
        text = py.read_text()
        assert ".tsv" not in text or "read_text" in text, f"{py.name} may be writing the ledger"
