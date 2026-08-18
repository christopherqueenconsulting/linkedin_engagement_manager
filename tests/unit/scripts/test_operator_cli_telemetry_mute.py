"""Pins issue #1661: an operator CLI in `scripts/` must mute its own telemetry before it imports.

The corpus samplers and the measurement scripts read PRODUCTION data and each say so in their
docstring — "run it where a database is reachable", which an agent worktree is not. `lem-agentd`
loads `agent-pipeline/secrets.env` as a systemd `EnvironmentFile`, so every run it spawns gets a
real `POSTHOG_API_KEY` and no MySQL credentials: one sampler run on the VPS host had
`get_active_user_ids()` catch `ProgrammingError: 1045 (28000): Access denied for user 'lem_user'`,
publish it as a grouped `$exception`, and the daily error->issue cron file it as a GitHub issue
against production code that was working.

Two things have to hold, and both are asserted on the SOURCE rather than on an imported module:
the mute is set at import time, so by the time a test could observe `os.environ` the damage (or
the fix) has already happened, and re-importing these modules to watch it would need the very
database they do not have.
"""

import pathlib
import re

import pytest

pytestmark = pytest.mark.unit

#: Every `scripts/` CLI that reads production data through the `cqc_lem` repository seam.
OPERATOR_CLIS = [
    "scripts/sample_newsletter_scaffolds.py",
    "scripts/sample_newsletter_similarity.py",
    "scripts/sample_shipped_videos.py",
    "scripts/measure_proof_gate_impact.py",
]

_MUTE = re.compile(r'^os\.environ\.setdefault\(\s*"LEM_TELEMETRY_MUTED"\s*,\s*"1"\s*\)',
                   re.MULTILINE)
_CQC_IMPORT = re.compile(r"^(?:from|import)\s+cqc_lem\b", re.MULTILINE)


@pytest.mark.parametrize("path", OPERATOR_CLIS)
def test_the_cli_mutes_its_own_telemetry(path):
    source = pathlib.Path(path).read_text(encoding="utf-8")

    assert _MUTE.search(source), (
        f"{path} reads production data but never sets LEM_TELEMETRY_MUTED — a run without "
        f"database credentials will file a production error-tracking issue (#1661)."
    )


@pytest.mark.parametrize("path", OPERATOR_CLIS)
def test_the_mute_is_set_before_cqc_lem_is_imported(path):
    """Order is the whole guard: the PostHog Logs handler is built when `logger` is imported."""
    source = pathlib.Path(path).read_text(encoding="utf-8")
    mute = _MUTE.search(source)
    first_import = _CQC_IMPORT.search(source)

    assert mute and first_import, f"{path} is missing the mute or the cqc_lem import"
    assert mute.start() < first_import.start(), (
        f"{path} sets LEM_TELEMETRY_MUTED after importing cqc_lem — the OTLP log handler is "
        f"already built by then (#1661)."
    )


def test_setdefault_not_assignment_so_an_operator_can_opt_back_in():
    """A run that SHOULD be recorded stays possible: `LEM_TELEMETRY_MUTED=0` in the environment."""
    for path in OPERATOR_CLIS:
        source = pathlib.Path(path).read_text(encoding="utf-8")
        assert 'os.environ["LEM_TELEMETRY_MUTED"]' not in source, (
            f"{path} overwrites the mute instead of defaulting it, so an operator cannot opt in."
        )
