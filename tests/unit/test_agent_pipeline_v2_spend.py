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


def test_a_missing_all_models_line_falls_back_to_the_worst_per_model_line():
    """The partial-parse hole: a qualifier rename must not silently drop the WEEK constraint.

    With no line matching "all models", the week used to stay None while the session line kept
    `readable=True` — so the ledger fallback was suppressed AND the owner's week rule was ignored.
    A per-model line is a floor on the week, and a floor is the safe direction for a spend gate.
    """
    u = spend.parse_usage(
        "Current session: 10% used\nCurrent week (all): 99% used · resets Aug 13, 6:59pm (UTC)"
    )
    assert u.readable is True
    assert u.week_pct == 99.0
    assert "Aug 13" in u.week_resets
    assert spend.state(usage=u).level == "exhausted"


# ---------------------------------------------------------------- thresholds


def _state(pct_week, **kw):
    return spend.state(usage=spend.parse_usage(f"Current week (all models): {pct_week}% used"), **kw)


@pytest.mark.parametrize("pct,level", [(0, "normal"), (49, "normal"), (50, "conserve"),
                                       (65, "conserve"), (84, "conserve"), (85, "exhausted"),
                                       (100, "exhausted")])
def test_thresholds(pct, level):
    assert _state(pct).level == level


def test_the_owners_50_percent_rule_engages_at_todays_real_usage():
    """65% measured on this box means the pipeline should already be conserving."""
    st = spend.state(usage=spend.parse_usage(REAL_OUTPUT))
    assert st.level == "conserve"
    assert st.constrained is True
    assert "65%" in st.reason


def test_session_window_can_bind_before_the_week():
    """A burst exhausts the 5h session long before the week — v2 runs agents concurrently."""
    st = spend.state(usage=spend.parse_usage(
        "Current session: 92% used\nCurrent week (all models): 20% used"))
    assert st.level == "exhausted"
    assert "session" in st.reason


# ---------------------------------------------------------------- routing


def test_normal_uses_claude_for_everything():
    st = _state(10)
    for mode in ("start", "fix", "docfix", "selfreview"):
        assert spend.choose_lane(mode, st)[0] == "claude"


def test_conserve_reserves_claude_for_priority_lanes_only():
    """The measured state right now: mechanical work moves, quality-critical work stays."""
    st = _state(65)
    assert spend.choose_lane("selfreview", st)[0] == "claude"
    assert spend.choose_lane("start", st)[0] == "claude"
    assert spend.choose_lane("docfix", st)[0] == "ollama"
    assert spend.choose_lane("review", st)[0] == "ollama"
    assert spend.choose_lane("fix", st)[0] == "ollama"


def test_exhausted_moves_even_selfreview():
    """A tier-3 review with a warning marker beats no merges at all (the H4 livelock)."""
    st = _state(90)
    assert spend.choose_lane("selfreview", st)[0] == "ollama"
    assert spend.choose_lane("start", st)[0] == "ollama"


def test_unavailable_ollama_always_yields_claude():
    """Degrading quality is acceptable; halting the pipeline is not."""
    lane, reason = spend.choose_lane("docfix", _state(95), ollama_available=False)
    assert lane == "claude" and reason == "ollama_unavailable"


def test_owner_pin_overrides_everything():
    lane, reason = spend.choose_lane("docfix", _state(95), pinned="claude")
    assert (lane, reason) == ("claude", "owner_pinned")


def test_every_route_explains_itself():
    """v1 could not answer 'why did it switch'; every decision here carries its numbers."""
    for pct in (10, 65, 95):
        _, reason = spend.choose_lane("docfix", _state(pct))
        assert reason


# ---------------------------------------------------------------- fallbacks


def test_unreadable_usage_falls_back_to_the_cost_ledger(tmp_path):
    ledger = tmp_path / "spend.ndjson"
    spend.record(ledger, mode="start", lane="claude", model="opus",
                 result={"total_cost_usd": 30.0}, run_seconds=60, rc=0)
    st = spend.state(usage=spend.Usage(readable=False), ledger=ledger, weekly_budget_usd=50.0)
    assert st.source == "cost_ledger"
    assert st.level == "conserve"  # 30/50 = 60%


def test_no_signal_at_all_does_not_silently_conserve(caplog):
    """A parse regression must not route everything to Ollama forever in silence."""
    with caplog.at_level("WARNING", logger="lemd.spend"):
        st = spend.state(usage=spend.Usage(readable=False))
    assert st.level == "normal"
    assert st.source == "unknown"
    # "Logs loudly" is the whole safety story for this branch — silence here is invisible drift.
    assert any("no usage signal" in r.message for r in caplog.records)


def test_unpriced_runs_are_charged_at_the_measured_mean(tmp_path):
    """`cost_known` was written and never read, so a killed run priced itself at $0.

    Two known $10 runs plus two that died before reporting: summing `cost_usd` says 40% of budget
    (normal), while the honest estimate is 80% (conserve).
    """
    ledger = tmp_path / "spend.ndjson"
    for _ in range(2):
        spend.record(ledger, mode="start", lane="claude", model="opus",
                     result={"total_cost_usd": 10.0}, run_seconds=60, rc=0)
    for _ in range(2):
        spend.record(ledger, mode="fix", lane="claude", model="opus", result=None,
                     run_seconds=2700, rc=-9)
    assert spend.ledger_summary(ledger, since=0) == (20.0, 2, 2)
    st = spend.state(usage=spend.Usage(readable=False), ledger=ledger, weekly_budget_usd=50.0)
    assert st.level == "conserve"  # (20 + 2*10) / 50 = 80%, not 40%
    assert "unpriced" in st.reason


def test_a_window_of_only_unpriced_runs_is_unknown_not_zero_percent(tmp_path):
    """No priced run means no mean to impute from — that is no signal, not '$0 spent'."""
    ledger = tmp_path / "spend.ndjson"
    spend.record(ledger, mode="fix", lane="claude", model="opus", result=None,
                 run_seconds=2700, rc=-9)
    st = spend.state(usage=spend.Usage(readable=False), ledger=ledger, weekly_budget_usd=50.0)
    assert st.source == "unknown"
    assert "0%" not in st.reason


def test_record_never_breaks_dispatch_when_the_ledger_is_unwritable(tmp_path):
    """Metering is a passenger: an unwritable ledger directory must not kill the run."""
    blocked = tmp_path / "afile"
    blocked.write_text("not a directory")
    spend.record(blocked / "nested" / "spend.ndjson", mode="fix", lane="claude", model="m",
                 result={"total_cost_usd": 1.0}, run_seconds=1, rc=0)


def test_record_marks_unknown_cost_distinctly(tmp_path):
    """A killed run costs something; recording it as 0 would make an expensive lane look free."""
    ledger = tmp_path / "spend.ndjson"
    spend.record(ledger, mode="fix", lane="claude", model="opus", result=None,
                 run_seconds=2700, rc=-9)
    row = json.loads(ledger.read_text().strip())
    assert row["cost_known"] is False
    assert row["cost_usd"] == 0.0
    assert row["run_seconds"] == 2700.0


def test_ledger_ignores_a_torn_final_line(tmp_path):
    """Appends can be interrupted; one bad line must not poison the sum."""
    ledger = tmp_path / "spend.ndjson"
    spend.record(ledger, mode="start", lane="claude", model="m",
                 result={"total_cost_usd": 1.5}, run_seconds=10, rc=0)
    with ledger.open("a") as fh:
        fh.write('{"ts":1,"lane":"claude","cost_u')  # torn write
    assert spend.ledger_cost(ledger, since=0) == 1.5


def test_ledger_separates_lanes(tmp_path):
    ledger = tmp_path / "spend.ndjson"
    spend.record(ledger, mode="fix", lane="claude", model="m",
                 result={"total_cost_usd": 2.0}, run_seconds=1, rc=0)
    spend.record(ledger, mode="fix", lane="ollama", model="t2",
                 result={"total_cost_usd": 9.0}, run_seconds=1, rc=0)
    assert spend.ledger_cost(ledger, since=0, lane="claude") == 2.0


# ---------------------------------------------------------------- caching


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


def test_parse_cli_result_takes_the_last_object():
    out = 'noise\n{"unrelated":1}\n{"total_cost_usd":0.14,"num_turns":1}\n'
    assert spend.parse_cli_result(out)["total_cost_usd"] == 0.14


def test_parse_cli_result_none_when_absent():
    assert spend.parse_cli_result("no json here") is None
