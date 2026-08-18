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

import importlib.util
import os
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


class TestTheMuteDoesNotLeakIntoOtherTests:
    """The mute is a PROCESS-wide env var, and importing a CLI here runs it in the pytest process.

    Until the `_telemetry_not_muted` guard in tests/unit/conftest.py, executing one of these
    modules left `LEM_TELEMETRY_MUTED=1` set for the whole session, so every later test asserting
    on `posthog.capture` read `call_args` off a mock a muted `_emit` never called. These two run in
    file order: the first creates the leak, the second proves the lane cleared it.
    """

    def test_importing_a_cli_sets_the_mute_in_this_process(self):
        spec = importlib.util.spec_from_file_location(
            "sample_newsletter_scaffolds_leak_probe", OPERATOR_CLIS[0])
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        assert os.environ.get("LEM_TELEMETRY_MUTED") == "1"

    def test_the_next_test_starts_unmuted(self):
        assert "LEM_TELEMETRY_MUTED" not in os.environ, (
            "a CLI import leaked its mute into the rest of the lane (#1661)"
        )


#: Scripts that reach the same production data but where publishing IS the point, each with the
#: reason it is not muted. They run INSIDE a production container (piped in on stdin) or as a
#: production cron, so their `$exception` is a real production signal, not an agent-worktree
#: artefact — the distinction the whole guard turns on.
TELEMETRY_IS_THE_POINT = {
    "scripts/linkedin_live_validation.py":
        "read-only probe piped into celery_worker_selenium; grades PRODUCTION Selenium",
    "scripts/linkedin_post_stats_api_probe.py":
        "token probe piped into celery_worker; answers a question about the live account",
    "scripts/linkedin_version_check.py":
        "weekly production cron — a retired LI_API_VERSION must reach error tracking",
    "scripts/reseed_own_post_comments.py":
        "one-off operator backfill that WRITES to LinkedIn from a machine with real credentials",
}

#: The repository seam a script has to cross to need one of those two lists. `platform.db.enums` is
#: deliberately absent: it is pure types with zero I/O, so importing it cannot raise a credentials
#: error (see `src/cqc_lem/domain/`, the same domain-free rule).
_DB_SEAM = re.compile(r"cqc_lem\.(?:utilities\.db|platform\.db\.repositories)\b")


def _scripts_reading_production_data() -> set:
    """Every `scripts/*.py` that imports the DB facade, discovered rather than listed."""
    return {str(path) for path in sorted(pathlib.Path("scripts").glob("*.py"))
            if _DB_SEAM.search(path.read_text(encoding="utf-8"))}


class TestEveryDbReadingScriptHasMadeTheChoice:
    """The rule, not just today's four files (#1661).

    `docs/error-tracking.md` says "add the same `setdefault` line to any new operator CLI that
    reads production data" — prose no check enforces, which is how this class of bug recurs. A
    script that crosses the DB seam must appear in EXACTLY one of the two lists above, so adding
    one is a decision someone had to write down rather than a default nobody noticed.
    """

    def test_no_db_reading_script_is_unclassified(self):
        classified = set(OPERATOR_CLIS) | set(TELEMETRY_IS_THE_POINT)
        unclassified = _scripts_reading_production_data() - classified

        assert not unclassified, (
            f"{sorted(unclassified)} read production data through cqc_lem.utilities.db but are in "
            f"neither OPERATOR_CLIS nor TELEMETRY_IS_THE_POINT. Add the "
            f'`os.environ.setdefault("LEM_TELEMETRY_MUTED", "1")` line and list it in the first, '
            f"or list it in the second with the reason its telemetry is wanted (#1661)."
        )

    def test_both_lists_name_files_that_exist_and_still_cross_the_seam(self):
        """A rename or a dropped import must empty the guard loudly, never silently."""
        reading = _scripts_reading_production_data()
        for path in list(OPERATOR_CLIS) + list(TELEMETRY_IS_THE_POINT):
            assert pathlib.Path(path).is_file(), f"{path} is listed but does not exist"
            assert path in reading, (
                f"{path} no longer imports the DB facade — drop it from the list rather than "
                f"leaving a guard that pins nothing (#1661)."
            )

    def test_the_lists_are_disjoint(self):
        assert not set(OPERATOR_CLIS) & set(TELEMETRY_IS_THE_POINT)

    @pytest.mark.parametrize("path", sorted(TELEMETRY_IS_THE_POINT))
    def test_an_allowlisted_script_really_is_unmuted(self, path):
        """Otherwise the allowlist drifts into a list of files nobody re-read."""
        source = pathlib.Path(path).read_text(encoding="utf-8")

        assert not _MUTE.search(source), (
            f"{path} now mutes its telemetry — move it to OPERATOR_CLIS so the two lists keep "
            f"meaning what they say (#1661)."
        )
