"""The PostHog key the weekly model-health cron hands to the benchmark (issue #1453).

`scripts/weekly_model_check.sh` sources a narrow allowlist of variables out of `/opt/lem/.env`
before running `benchmark_models.py`. A variable that is not on that list simply never reaches the
run — and the benchmark degrades to its in-runner judge silently — so the allowlist is checked
statically here rather than by executing 250 lines of orchestration that shell out to docker.
"""

import pathlib
import re

import pytest

pytestmark = pytest.mark.unit

_SH = pathlib.Path(__file__).resolve().parents[3] / "scripts" / "weekly_model_check.sh"
_SOURCE_LINE = re.compile(r'^\s*set -a; source <\(sudo -n grep -E "(?P<pattern>[^"]+)"', re.M)


def _allowlist() -> str:
    """The grep pattern on the sourcing line that feeds the BENCHMARK step.

    The script sources twice (the provider creds first, the benchmark env later), so the line is
    picked by content rather than by position.
    """
    patterns = [m.group("pattern") for m in _SOURCE_LINE.finditer(_SH.read_text(encoding="utf-8"))
                if "POSTHOG" in m.group("pattern")]
    assert len(patterns) == 1, "the benchmark env-sourcing line moved — update this test with it"
    return patterns[0]


class TestBenchmarkKeySourcing:
    def test_the_scoped_benchmark_key_is_sourced(self):
        # Owner decision 1A: this lane's own purpose-scoped key.
        assert "POSTHOG_BENCHMARK_API_KEY" in _allowlist()

    def test_the_revoked_shared_key_is_no_longer_sourced(self):
        # The shared key was revoked 2026-08-31 (issue #1453). Sourcing it now would only export a
        # dead credential into the benchmark lane, where posthog_keys.py would prefer it over
        # nothing and the run would 401 instead of falling cleanly to the in-runner judge.
        assert "POSTHOG_PERSONAL_API_KEY" not in _allowlist()

    def test_the_project_key_that_emits_the_scored_events_is_still_sourced(self):
        # Personal key without POSTHOG_API_KEY scores nothing: both halves or the fallback judge.
        assert "POSTHOG_API_KEY" in _allowlist()

    def test_the_scoped_key_is_named_explicitly_not_left_to_a_prefix_match(self):
        # `BENCHMARK_[A-Z_]+` is anchored at ^, so it never matches POSTHOG_BENCHMARK_API_KEY.
        pattern = _allowlist()
        alternatives = pattern.strip("^()=").split("|")
        assert "POSTHOG_BENCHMARK_API_KEY" in [a.strip() for a in alternatives]
