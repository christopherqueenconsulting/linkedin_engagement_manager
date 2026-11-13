"""The model router (#2560): decision tables for `route.decide`, its `lane.decide` hook, and `lane_for.py`.

Each rule is a table row, because the failure this router exists to prevent is a routing choice
nobody can explain afterwards. Acceptance boxes 1-6 live here; box 7 (scorer vs human ratings)
reads a committed fixture and waits until the owner has rated the candidate issues.
"""

from __future__ import annotations

import itertools
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_V2 = _ROOT / "scripts" / "agent-pipeline" / "v2"
sys.path.insert(0, str(_V2))

from lemd import lane, route, spend, usage_signal  # noqa: E402

pytestmark = pytest.mark.unit

ENV: dict[str, str] = {}
SONNET, OPUS = route.DEFAULTS["LEM_ROUTER_SONNET_MODEL"], route.DEFAULTS["LEM_ROUTER_OPUS_MODEL"]
REVIEW = route.DEFAULTS["LEM_REVIEW_MODEL"]


def _sig(claude: float | None = 10.0, codex5h: float | None = 10.0, codexw: float | None = 10.0,
         status: str | None = "ok") -> usage_signal.Signal:
    """A readable quota signal."""
    return usage_signal.Signal(readable=True, reason="ok", age_s=1.0, claude_weekly_pct=claude,
                               claude_status=status, codex_5h_pct=codex5h, codex_weekly_pct=codexw)


def _ctx(mode: str, *, labels=(), signal=None, flag="full", **kw) -> route.Context:
    """A router context with inputs taken from keyword args."""
    inputs = route.Inputs(mode=mode, labels=frozenset(labels),
                          **{k: v for k, v in kw.items() if k in route.Inputs.__dataclass_fields__})
    rest = {k: v for k, v in kw.items() if k not in route.Inputs.__dataclass_fields__}
    return route.Context(flag=flag, inputs=inputs, signal=signal or _sig(), env=ENV, **rest)


def _pick(*args, **kw) -> tuple[str, str]:
    """(harness, model) for a context."""
    h, m, _ = route.decide(_ctx(*args, **kw))
    return h, m


# --------------------------------------------------------------------------- labels win

@pytest.mark.parametrize("mode", ["start", "review", "selfreview", "depmajor", "docfix", "fix"])
@pytest.mark.parametrize("extra", [
    {}, {"attempt": 3}, {"body_chars": 50000}, {"labels": ("risk:high", "priority:high")},
])
def test_model_label_beats_every_other_input(mode, extra):
    labels = ("agent:model:haiku", *extra.pop("labels", ()))
    harness, model, reason = route.decide(
        _ctx(mode, labels=labels, signal=_sig(claude=99, codex5h=99), **extra))
    assert (harness, model) == ("claude", route.DEFAULTS["LEM_ROUTER_HAIKU_MODEL"])
    assert "arm=label_model" in reason


@pytest.mark.parametrize("mode", ["start", "review", "selfreview", "depmajor"])
def test_tier_label_pins_ollama_and_a_model_label_outranks_it(mode):
    assert _pick(mode, labels=("agent:tier:3",)) == ("ollama", "lem-agent-tier3")
    assert _pick(mode, labels=("agent:tier:2-alt",)) == ("ollama", "lem-agent-tier2-alt")
    assert _pick(mode, labels=("agent:tier:3", "agent:model:opus"))[0] == "claude"


# --------------------------------------------------------------------------- two-family review

def test_codex_reviews_first_on_every_pr():
    assert _pick("review") == ("codex", REVIEW)
    assert _pick("selfreview") == ("codex", REVIEW)
    assert _pick("review", labels=("risk:high",)) == ("codex", REVIEW), "Codex is first even on risky PRs"


@pytest.mark.parametrize("labels", [("risk:high",), ("risk:security",), ("risk:migration",),
                                    ("risk:live-linkedin",), ("risk:auth",)])
def test_risky_paths_get_a_sonnet_review_on_top(labels):
    assert _pick("selfreview", labels=labels) == ("claude", SONNET)


def test_protected_paths_and_depmajor_get_the_second_family():
    assert _pick("selfreview", protected_path=True) == ("claude", SONNET)
    assert _pick("depmajor") == ("claude", SONNET)
    assert route.decide(_ctx("depmajor", signal=_sig(claude=99, status="exhausted")))[:2] == ("claude", SONNET)


def test_no_gpt_model_on_the_claude_harness_and_no_claude_model_on_codex():
    for mode, labels, sig in itertools.product(
            ("start", "review", "selfreview", "depmajor", "fix"),
            ((), ("risk:high",)), (_sig(), _sig(claude=99), _sig(codex5h=99, codexw=99))):
        harness, model, _ = route.decide(_ctx(mode, labels=labels, signal=sig))
        if harness == "claude":
            assert not model.startswith(("gpt", "codex")), (mode, labels, model)
        if harness == "codex":
            assert not model.startswith(("claude", "sonnet", "opus", "haiku")), (mode, labels, model)


def test_gpt_6_luna_is_never_a_reviewer():
    ctx = route.Context(flag="full", inputs=route.Inputs(mode="review"), signal=_sig(),
                        env={"LEM_REVIEW_MODEL": "gpt-6-luna"})
    assert route.decide(ctx)[1] == REVIEW
    assert "gpt-6-luna" not in route.allowed_models({"LEM_REVIEW_MODEL": "gpt-6-luna"})


def test_the_alternate_review_model_is_a_config_value():
    ctx = route.Context(flag="full", inputs=route.Inputs(mode="review"), signal=_sig(),
                        env={"LEM_REVIEW_MODEL": "gpt-5.6-luna"})
    assert route.decide(ctx)[:2] == ("codex", "gpt-5.6-luna")


# --------------------------------------------------------------------------- code defaults + Opus

def test_code_defaults_to_sonnet():
    assert _pick("start") == ("claude", SONNET)
    assert _pick("start", labels=("priority:low",)) == ("claude", SONNET)


@pytest.mark.parametrize("kw", [{"attempt": 2}, {"body_chars": 9000, "planned_files": 4},
                                {"ci_failures": 4}, {"conflicts": 6}])
def test_high_complexity_or_a_failed_attempt_escalates_to_opus(kw):
    harness, model, reason = route.decide(_ctx("start", **kw))
    assert (harness, model) == ("claude", OPUS)
    assert "code_opus_escalation" in reason


def test_score_is_monotonic_in_every_count():
    base = route.Inputs(mode="start")
    for field in ("body_chars", "planned_files", "ci_failures", "conflicts"):
        low = route.score(base, ENV)
        high = route.score(route.Inputs(mode="start", **{field: 10}), ENV)
        assert high > low, field


def test_weights_and_threshold_come_from_config():
    env = {"LEM_ROUTER_OPUS_THRESHOLD": "0.5", "LEM_ROUTER_W_FILES": "1"}
    ctx = route.Context(flag="full", inputs=route.Inputs(mode="start", planned_files=1), env=env)
    assert route.decide(ctx)[:2] == ("claude", OPUS)
    junk = route.Context(flag="full", inputs=route.Inputs(mode="start"), env={"LEM_ROUTER_W_BODY": "x"})
    assert route.decide(junk)[:2] == ("claude", SONNET), "a junk weight falls back, never crashes"


def test_mechanical_modes_keep_the_cheap_lane():
    for mode in ("phasefix", "docfix", "depfix"):
        assert _pick(mode) == ("ollama", "lem-agent-tier2")


# --------------------------------------------------------------------------- fallback chains

def test_every_chain_ends_at_ollama():
    for kind in ("code", "review", "claude_review"):
        assert route._chain(kind, route._models(ENV))[-1][0] == "ollama", kind


def test_code_chain_walks_opus_then_sonnet_then_ollama():
    assert _pick("start", attempt=2) == ("claude", OPUS)
    assert _pick("start", attempt=2, bad_models=frozenset({OPUS})) == ("claude", SONNET)
    assert _pick("start", attempt=2, bad_models=frozenset({OPUS, SONNET})) == ("ollama", "lem-agent-tier2")
    assert _pick("start", signal=_sig(claude=50)) == ("ollama", "lem-agent-tier2"), "weekly flip"
    assert _pick("start", paused=frozenset({"claude"})) == ("ollama", "lem-agent-tier2")


def test_review_chain_walks_codex_then_sonnet_then_ollama_tier3():
    assert _pick("review", signal=_sig(codex5h=95)) == ("claude", SONNET)
    assert _pick("review", signal=_sig(codexw=95)) == ("claude", SONNET)
    assert _pick("review", proxy_down=True) == ("claude", SONNET)
    assert _pick("review", bad_models=frozenset({REVIEW})) == ("claude", SONNET)
    assert _pick("review", signal=_sig(claude=60, codex5h=95)) == ("ollama", "lem-agent-tier3")
    assert _pick("selfreview", labels=("risk:high",), signal=_sig(claude=60)) == ("ollama", "lem-agent-tier3")


def test_a_fallback_names_what_it_skipped():
    reason = route.decide(_ctx("review", signal=_sig(codex5h=95)))[2]
    assert f"fell_back=[codex/{REVIEW}:codex_over_budget 5h=95%]" in reason


@pytest.mark.parametrize("paused,other", [("claude", "review"), ("codex", "start")])
def test_a_pause_on_one_provider_does_not_move_another(paused, other):
    assert _pick(other) == _pick(other, paused=frozenset({paused}))


def test_a_claude_budget_does_not_move_a_codex_decision_and_vice_versa():
    assert _pick("review", signal=_sig(claude=99)) == _pick("review")
    assert _pick("start", signal=_sig(codex5h=99, codexw=99)) == _pick("start")


def test_an_unreadable_signal_gives_the_deterministic_default():
    for mode in ("start", "review", "selfreview", "fix"):
        for sig in (usage_signal.UNREADABLE, usage_signal.Signal(False, "stale", 999.0),
                    usage_signal.Signal(False, "malformed", None)):
            assert route.decide(_ctx(mode, signal=sig)) == route.decide(_ctx(mode, signal=_sig(0, 0, 0)))


def test_every_input_combination_returns_an_allowed_model():
    allowed = route.allowed_models(ENV)
    labels = [(), ("priority:high",), ("risk:high",), ("agent:model:opus",), ("agent:tier:1",)]
    sigs = [usage_signal.UNREADABLE, _sig(), _sig(99, 99, 99), _sig(None, None, None, status=None),
            _sig(status="exhausted")]
    for mode, lab, sig, attempt, paused, down in itertools.product(
            ("start", "revise", "rebase", "fix", "review", "selfreview", "depmajor", "docfix", "weird"),
            labels, sigs, (1, 3), (frozenset(), frozenset({"claude", "codex"})), (False, True)):
        harness, model, reason = route.decide(_ctx(mode, labels=lab, signal=sig, attempt=attempt,
                                                   paused=paused, proxy_down=down))
        assert model in allowed, (mode, lab, harness, model)
        assert harness in ("claude", "codex", "ollama")
        assert reason.startswith("route arm=")


# --------------------------------------------------------------------------- lane.decide hook

def _usage(week: float) -> spend.Usage:
    return spend.Usage(week_pct=week, session_pct=10.0, readable=True, week_resets="Aug 13")


@pytest.mark.parametrize("mode", ["start", "revise", "rebase", "review", "selfreview", "depmajor", "docfix", "fix"])
@pytest.mark.parametrize("week", [10.0, 60.0])
def test_off_is_exactly_todays_routing(mode, week):
    off = lane.decide(mode, _usage(week), router=_ctx(mode, flag="off"))
    assert off == lane.decide(mode, _usage(week))
    assert off.model is None and off.arm == ""
    assert lane.emit_env(off) == lane.emit_env(lane.decide(mode, _usage(week)))
    assert "LEM_LANE_MODEL" not in lane.emit_env(off)


def test_shadow_logs_but_leaves_every_launch_assignment_unchanged(caplog):
    plain = lane.emit_env(lane.decide("start", _usage(10.0)))
    with caplog.at_level("INFO", logger="lemd.lane"):
        shadow = lane.decide("start", _usage(10.0), router=_ctx("start", flag="shadow", attempt=2))
    assert "router[shadow]" in caplog.text and "arm=code_opus_escalation" in caplog.text
    env = lane.emit_env(shadow)
    assert "LEM_ROUTE_ARM='shadow route arm=code_opus_escalation" in env
    assert "LEM_LANE_MODEL" not in env
    assert [ln for ln in env.splitlines() if not ln.startswith("LEM_ROUTE_ARM")] == plain.splitlines()


def test_reviews_acts_on_reviews_only():
    code = lane.decide("start", _usage(10.0), router=_ctx("start", flag="reviews", attempt=2))
    assert code.model is None and code.arm.startswith("not_acting")
    rev = lane.decide("selfreview", _usage(10.0), router=_ctx("selfreview", flag="reviews",
                                                               labels=("risk:high",)))
    assert (rev.lane, rev.model) == ("claude", SONNET)
    env = lane.emit_env(rev)
    assert f"LEM_LANE_MODEL='{SONNET}'" in env and "LEM_LANE_HARNESS='claude'" in env


def test_full_acts_on_code_and_sets_the_model():
    choice = lane.decide("start", _usage(10.0), router=_ctx("start", flag="full", attempt=2))
    assert (choice.lane, choice.model) == ("claude", OPUS)


def test_a_codex_decision_falls_back_to_the_existing_path_until_phase_3():
    legacy = lane.decide("review", _usage(10.0))
    for flag in ("reviews", "full"):
        got = lane.decide("review", _usage(10.0), router=_ctx("review", flag=flag))
        assert (got.lane, got.tier, got.model, got.reason) == (legacy.lane, legacy.tier, None, legacy.reason)
        assert "codex_pending_phase3" in got.arm and f"codex/{REVIEW}" in got.arm


def test_router_never_overrides_the_weekly_flip_back_onto_claude():
    legacy = lane.decide("start", _usage(95.0))
    assert legacy.lane == "ollama"
    got = lane.decide("start", _usage(95.0), router=_ctx("start", flag="full"))
    assert (got.lane, got.tier, got.model) == (legacy.lane, legacy.tier, None)
    assert "overridden_by_weekly_flip" in got.arm


def test_a_review_that_falls_to_tier3_carries_the_degraded_marker():
    got = lane.decide("review", _usage(10.0), router=_ctx("review", flag="full",
                                                           signal=_sig(claude=60, codex5h=95)))
    assert (got.lane, got.tier) == ("ollama", "lem-agent-tier3")
    assert "DEGRADED" in got.marker


# --------------------------------------------------------------------------- lane_for.py, DRY_RUN

def _run_lane_for(tmp_path: Path, mode: str, **env: str) -> subprocess.CompletedProcess:
    """Run `lane_for.py` the way `agent_run.sh` does, with a stubbed environment and no network."""
    state = tmp_path / "state"
    state.mkdir(exist_ok=True)
    clean = {k: v for k, v in os.environ.items() if not k.startswith(("LEM_", "ISSUE", "ATTEMPTS"))}
    clean.update({"BASE": str(tmp_path), "DRY_RUN": "1", **env})
    return subprocess.run([sys.executable, str(_V2 / "lane_for.py"), mode], env=clean,
                          capture_output=True, text=True, timeout=30, check=False)


def test_a_dry_run_shadow_tick_logs_a_decision_without_changing_the_launch(tmp_path):
    facts = {"LEM_ROUTE_LABELS": "priority:high risk:high", "LEM_ROUTE_ATTEMPT": "2"}
    off = _run_lane_for(tmp_path, "start", **facts)
    shadow = _run_lane_for(tmp_path, "start", LEM_ROUTER="shadow", **facts)
    assert off.returncode == shadow.returncode == 0
    assert "LEM_ROUTE_ARM" not in off.stdout
    launch = [ln for ln in shadow.stdout.splitlines() if not ln.startswith("LEM_ROUTE_ARM")]
    assert launch == off.stdout.splitlines()
    arm = next(ln for ln in shadow.stdout.splitlines() if ln.startswith("LEM_ROUTE_ARM"))
    assert "arm=code_opus_escalation" in arm


def test_lane_for_with_no_router_flag_is_off(tmp_path):
    out = _run_lane_for(tmp_path, "start")
    assert out.stdout.strip() == "LEM_LANE_REASON='probe_unavailable'"


def test_lane_for_reads_the_phase_1_quota_cache(tmp_path):
    (tmp_path / "state").mkdir()
    import time
    usage_signal.write_cache(tmp_path / "state" / "usage_signal.json", {
        "fetched_at": time.time(), "claude": {"weekly_pct": 10.0, "status": "ok", "reset_at": None},
        "codex": {"five_hour_pct": 99.0, "weekly_pct": 10.0, "reset_at": None}})
    out = _run_lane_for(tmp_path, "review", LEM_ROUTER="full", LEM_ROUTE_LABELS="")
    env = dict(ln.split("=", 1) for ln in out.stdout.splitlines())
    assert env["LEM_LANE_MODEL"] == f"'{SONNET}'", out.stdout
    assert "fell_back=[codex/" in env["LEM_ROUTE_ARM"]


@pytest.mark.parametrize("value,expected", [("", "off"), ("bogus", "off"), ("SHADOW", "shadow"),
                                            ("reviews", "reviews"), ("full", "full"), ("off", "off")])
def test_router_flag_parsing_defaults_to_off(value, expected):
    assert route.router_mode({"LEM_ROUTER": value}) == expected


def test_dispatch_sh_prefers_the_router_model_and_agent_run_forwards_the_knobs():
    pipe = _ROOT / "scripts" / "agent-pipeline"
    assert 'AGENT_MODEL="${LEM_LANE_MODEL:-$claude_hint}"' in (pipe / "lib" / "dispatch.sh").read_text()
    run = (pipe / "v2" / "actions" / "agent_run.sh").read_text()
    assert "${!LEM_ROUTER*}" in run and "LEM_LANE_MODEL" in run


# --------------------------------------------------------------------------- scorer vs human ratings

_FIXTURE = _ROOT / "tests" / "fixtures" / "agent_pipeline" / "router_ratings.json"


@pytest.mark.skipif(not _FIXTURE.is_file(), reason="owner ratings of the 20 candidate issues not committed yet")
def test_scorer_agrees_with_human_ratings_on_at_least_18_of_20():
    rows = json.loads(_FIXTURE.read_text())
    assert len(rows) >= 20

    def bucket(points: float) -> str:
        return "hard" if points >= route._num(ENV, "LEM_ROUTER_OPUS_THRESHOLD") else (
            "medium" if points >= route._num(ENV, "LEM_ROUTER_OPUS_THRESHOLD") / 2 else "simple")

    agree = sum(bucket(route.score(route.Inputs(
        mode="start", labels=frozenset(r.get("labels", [])), body_chars=r["body_chars"],
        planned_files=r["planned_files"]), ENV)) == r["rating"] for r in rows)
    assert agree >= 18, f"{agree}/{len(rows)} agree"
