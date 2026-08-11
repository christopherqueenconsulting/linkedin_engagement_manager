"""Tests for usage-driven lane routing.

The rule the owner always wanted — "stop using the Claude subscription once the week is half gone"
— was never implementable in v1, whose own header states that no API exposes remaining quota. It
does exist: `claude -p "/usage"` answers headlessly. These tests pin the parse (a product surface
that can change), the escalation, and the fallbacks, because the failure that matters is a format
change silently reading as "0% used" and spending the entire window.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_V2 = Path(__file__).resolve().parents[2] / "scripts" / "agent-pipeline" / "v2"
sys.path.insert(0, str(_V2))

from lemd import spend  # noqa: E402

REAL_OUTPUT = """You are currently using your subscription to power your Claude Code usage

Current session: 19% used · resets Aug 10, 4:39am (UTC)
Current week (all models): 65% used · resets Aug 13, 6:59pm (UTC)
Current week (Fable): 30% used · resets Aug 13, 6:59pm (UTC)

What's contributing to your limits usage?
Approximate, based on local sessions on this machine — does not include other devices or claude.ai.
"""


# ---------------------------------------------------------------- parsing


def test_parses_the_real_cli_output():
    """Captured verbatim from this box, so a wording change fails here first."""
    u = spend.parse_usage(REAL_OUTPUT)
    assert u.readable is True
    assert u.session_pct == 19.0
    assert u.week_pct == 65.0
    assert u.week_model_pct == 30.0
    assert "Aug 13" in u.week_resets


def test_worst_pct_is_the_binding_window():
    u = spend.parse_usage(REAL_OUTPUT)
    assert u.worst_pct == 65.0  # week, not session
    assert spend.parse_usage("Current session: 90% used\nCurrent week (all models): 10% used").worst_pct == 90.0


def test_unparseable_output_is_not_zero_percent():
    """A format change must read as UNKNOWN, never as 'plenty left'."""
    for text in ("", "something else entirely", "Usage: 65 percent"):
        u = spend.parse_usage(text)
        assert u.readable is False
        assert u.worst_pct is None


def test_per_model_line_is_not_the_constraint():
    """The all-models number gates the subscription; a per-model line must not override it."""
    u = spend.parse_usage(
        "Current week (all models): 40% used\nCurrent week (Fable): 95% used"
    )
    assert u.week_pct == 40.0 and u.week_model_pct == 95.0
    assert u.worst_pct == 40.0


def test_usage_is_cached_so_the_meter_is_not_itself_a_cost(tmp_path):
    """The probe spends a real run; consulting it per dispatch would be self-defeating."""
    calls = []

    def fake_probe():
        calls.append(1)
        return spend.parse_usage(REAL_OUTPUT)

    cache = tmp_path / "usage.json"
    a = spend.cached_usage(cache, probe=fake_probe)
    b = spend.cached_usage(cache, probe=fake_probe)
    assert a.week_pct == b.week_pct == 65.0
    assert len(calls) == 1  # second read came from cache


def test_stale_cache_triggers_a_re_probe(tmp_path):
    calls = []

    def fake_probe():
        calls.append(1)
        return spend.parse_usage(REAL_OUTPUT)

    cache = tmp_path / "usage.json"
    spend.cached_usage(cache, probe=fake_probe, now=1000)
    spend.cached_usage(cache, probe=fake_probe, now=1000 + spend.USAGE_TTL + 1)
    assert len(calls) == 2


def test_unreadable_probe_is_not_cached(tmp_path):
    """Caching a failure would freeze the meter blind until the TTL expired."""
    cache = tmp_path / "usage.json"
    spend.cached_usage(cache, probe=lambda: spend.Usage(readable=False))
    assert not cache.exists()


def test_corrupt_cache_re_probes(tmp_path):
    cache = tmp_path / "usage.json"
    cache.write_text("{not json")
    got = spend.cached_usage(cache, probe=lambda: spend.parse_usage(REAL_OUTPUT))
    assert got.week_pct == 65.0


@pytest.mark.parametrize("content", ["null", "[]", '"nope"', "17"])
def test_valid_json_that_is_not_an_object_re_probes(content, tmp_path):
    """These raise AttributeError, which the corrupt-cache handler does not catch."""
    cache = tmp_path / "usage.json"
    cache.write_text(content)
    got = spend.cached_usage(cache, probe=lambda: spend.parse_usage(REAL_OUTPUT))
    assert got.week_pct == 65.0


def test_cache_write_is_atomic(tmp_path):
    """A concurrent reader must never see a half-written cache: a torn read costs a real probe."""
    cache = tmp_path / "usage.json"
    spend.cached_usage(cache, probe=lambda: spend.parse_usage(REAL_OUTPUT))
    assert json.loads(cache.read_text())["week_pct"] == 65.0
    assert not (tmp_path / "usage.json.new").exists()  # temp swapped, not left behind


# ---------------------------------------------------------------- CLI result parsing

