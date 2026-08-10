"""Per-mode budgets and timeouts, and a READ-ONLY view of the shared run ledger.

Two rules this module exists to hold:

**The TSV ledger is the only budget store.** `lib/ledger.sh` owns every write; nothing here writes.
v1 and v2 both charge through that script so a cutover — or a rollback — carries budget state in
both directions with no translation. A second store in SQLite would drift into one PR parked at two
attempts and another retried forever, and rollback safety depends on the TSV specifically.

**Reading is advisory, charging is authoritative.** The daemon reads counts here to avoid spawning a
process that is already out of budget, but the dispatch action re-checks and charges inside the
branch flock. So the read being a moment stale can only ever waste one spawn, never overspend: the
authoritative check-and-charge is atomic against both runners.

The parity between this reader and `lib/ledger.sh` is asserted by a test that generates files with
the shell and reads them back here — two implementations of one format is a drift hazard, and the
answer is a test that fails when they disagree, not a comment asking future readers to be careful.
"""

from __future__ import annotations

import logging
from pathlib import Path

LOG = logging.getLogger("lemd.policy")

#: Runs one (item, mode) may consume before the lane parks it for the owner.
#:
#: Charged at DISPATCH, so a timeout, a max-turns exhaustion and a pre-commit crash each consume
#: one. v1 counted commits instead, which made precisely the runs a budget exists to stop repeating
#: — the ones that die before committing — free.
MODE_BUDGET: dict[str, int] = {
    "start": 3,
    "fix": 4,
    "review": 3,
    "selfreview": 2,
    "rebase": 2,
    "revise": 2,
    "depfix": 3,
    "docfix": 3,
    "phasefix": 2,
    "merge": 3,
}

#: Wall-clock ceiling per mode, in seconds. The daemon enforces these with killpg; `run_lane`'s own
#: `timeout` is the belt-and-braces layer underneath.
MODE_TIMEOUT: dict[str, int] = {
    "start": 2700,
    "fix": 1200,
    "review": 900,
    "selfreview": 1200,
    "rebase": 1200,
    "revise": 1500,
    "depfix": 1200,
    "docfix": 600,
    "phasefix": 600,
}

DEFAULT_BUDGET = 3
DEFAULT_TIMEOUT = 1800

#: Modes whose budget key is the head SHA rather than the item's lifetime. Exactly one, and the
#: reason is specific: the merge QUEUE judges heads, so a new head is genuinely a new question.
#: Everywhere else a per-head key would let an agent refill its own meter by pushing a commit
#: (finding H1) — the failure that turns a 4-run budget into eight hours of ping-pong.
PER_HEAD_MODES = frozenset({"merge"})


def budget_for(mode: str) -> int:
    """Runs allowed for one mode."""
    return MODE_BUDGET.get(mode, DEFAULT_BUDGET)


def timeout_for(mode: str, *, occupancy: float = 0.0) -> int:
    """Wall-clock ceiling for one run, stretched when the box is busy.

    Args:
        mode: The run mode.
        occupancy: Fraction of agent slots already in use, 0.0-1.0.

    Returns:
        Seconds. Scaled up to 1.5x at full occupancy, because pytest under CPU contention gets
        slower and a timeout charged as a failed attempt would convert *starvation* into a
        `fix_exhausted` park — punishing an item for the scheduler's own concurrency (M6).
    """
    base = MODE_TIMEOUT.get(mode, DEFAULT_TIMEOUT)
    return int(base * (1.0 + 0.5 * max(0.0, min(1.0, occupancy))))


def ledger_path(base: str | Path, kind: str, number: int) -> Path:
    """Where `lib/ledger.sh` keeps one item's counters."""
    return Path(base) / "state" / "ledger" / f"{kind}-{number}.tsv"


def ledger_count(base: str | Path, kind: str, number: int, mode: str,
                 reset_key: str = "-") -> int:
    """Runs already charged for one (item, mode), as `lib/ledger.sh` would report them.

    Returns 0 for an absent file, an absent row, a malformed count, or a row whose stored reset key
    differs from the one asked for — a rotated key IS the reset, which is how an owner answer or a
    Dependabot rebase renews a budget that an agent's own pushes cannot.
    """
    path = ledger_path(base, kind, number)
    try:
        text = path.read_text(errors="replace")
    except OSError:
        return 0
    for line in text.splitlines():
        parts = line.split("\t")
        if len(parts) < 4 or parts[0] != mode:
            continue
        if parts[3] != reset_key:
            return 0
        try:
            return max(0, int(parts[1]))
        except ValueError:
            return 0
    return 0


def exhausted(base: str | Path, kind: str, number: int, mode: str,
              reset_key: str = "-") -> bool:
    """True when this (item, mode) has no runs left."""
    return ledger_count(base, kind, number, mode, reset_key) >= budget_for(mode)
