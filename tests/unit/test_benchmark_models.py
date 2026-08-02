"""Unit tests for scripts/benchmark_models.py — the model-tier evaluation harness (issue #721).

Everything here is the PURE half: suite loading, deterministic assertions, scorecard merge, the
champion/challenger gate, report + leaderboard rendering, judge planning/parsing, and the PostHog
evaluation specs. Provider and PostHog I/O are mocked.
"""

import importlib.util
import json
import pathlib
import sys
import time
from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.unit

_ROOT = pathlib.Path(__file__).resolve().parents[2]
_PATH = _ROOT / "scripts" / "benchmark_models.py"
_spec = importlib.util.spec_from_file_location("benchmark_models", _PATH)
bm = importlib.util.module_from_spec(_spec)
sys.modules["benchmark_models"] = bm
_spec.loader.exec_module(bm)

SUITE_DIR = _ROOT / "tests" / "benchmarks" / "model_tiers"


def _case(case_id="c1", assertions=None, **extra):
    case = {"id": case_id, "messages": [{"role": "user", "content": "hi"}],
            "assertions": assertions or [{"type": "max_chars", "value": 100}]}
    case.update(extra)
    return case


def _suite(tier="lem-simple", cases=None, thresholds=None):
    return {"tier": tier, "version": 1, "contract": "c", "judge_rubric": "r",
            "thresholds": bm.normalize_thresholds(thresholds),
            "cases": cases or [_case()]}


def _card(tier="lem-simple", model="m", role="candidate", det=1.0, judged=5, judge_rate=1.0,
          errors=None, contract=None):
    """A scorecard. `contract` defaults to `det` so a test that only cares about one rate reads the
    same either way; the two are set apart wherever the split (#910) is what's under test."""
    contract = det if contract is None else contract
    return {"tier": tier, "model": model, "role": role, "cases": 10,
            "deterministic_pass_rate": det, "deterministic_passed": int(round(det * 10)),
            "contract_pass_rate": contract, "contract_passed": int(round(contract * 10)),
            "repaired_cases": [], "errors": errors or [],
            "failed_cases": [], "unscored_assertions": 0, "judged": judged,
            "judge_passed": judged, "judge_timeouts": 0, "judge_pass_rate": judge_rate,
            "judge_notes": [], "latency_p50_ms": 100.0, "latency_p90_ms": 200.0,
            "total_tokens": 0, "case_results": [], "benchmark_run_id": "bm-x"}


# ───────────────────────────── the shipped fixtures ──────────────────────────────

class TestShippedSuites:
    def test_every_tier_has_a_loadable_suite_with_enough_cases(self):
        suites = bm.load_suites(str(SUITE_DIR))
        assert set(suites) == set(bm.TIERS)
        for tier, suite in suites.items():
            assert 10 <= len(suite["cases"]) <= 20, tier
            assert suite["contract"] and suite["judge_rubric"], tier

    def test_case_ids_are_unique_across_every_suite(self):
        seen = set()
        for suite in bm.load_suites(str(SUITE_DIR)).values():
            for case in suite["cases"]:
                assert case["id"] not in seen
                seen.add(case["id"])

    def test_fixtures_carry_only_synthetic_inputs(self):
        """No production data leakage: the suites are committed to a public repo."""
        banned = ("christopher.queen@", "linkedin.com/in/", "OLLAMA_CLOUD_API_KEY",
                  "sk-", "phc_", "@gmail.com")
        for path in SUITE_DIR.glob("*.json"):
            text = path.read_text()
            for needle in banned:
                assert needle not in text, f"{path.name} contains {needle!r}"

    def test_canned_outputs_satisfy_their_own_assertions(self):
        """The dry run is only a proof if the committed canned answers actually pass. A fixture
        that drifted from its assertions would make every dry run look broken."""
        for tier, suite in bm.load_suites(str(SUITE_DIR)).items():
            outputs = {c["id"]: (c.get("canned") or {}).get("output") for c in suite["cases"]}
            for result in bm.score_suite(suite, outputs):
                assert result["passes"], f"{tier}/{result['case_id']}: {result['failures']}"

    def test_judgeable_case_count_meets_each_suites_min_judged(self):
        for tier, suite in bm.load_suites(str(SUITE_DIR)).items():
            judgeable = [c for c in suite["cases"] if c.get("judge") is not False]
            assert len(judgeable) >= suite["thresholds"]["min_judged"], tier

    def test_router_suite_encodes_the_live_router_contract(self):
        suite = bm.load_suites(str(SUITE_DIR), ["lem-router"])["lem-router"]
        tiers = {a["value"] for c in suite["cases"] for a in c["assertions"]
                 if a["type"] == "router_tier"}
        assert tiers == {"lem-simple", "lem-medium", "lem-complex"}


# ─────────────────────────────── suite validation ────────────────────────────────

class TestLoadSuite:
    def test_rejects_unknown_tier(self):
        with pytest.raises(bm.SuiteError):
            bm.load_suite({"tier": "lem-nope", "cases": [_case()]})

    def test_rejects_unknown_assertion_type(self):
        with pytest.raises(bm.SuiteError, match="unknown assertion"):
            bm.load_suite({"tier": "lem-simple",
                           "cases": [_case(assertions=[{"type": "vibes"}])]})

    def test_rejects_duplicate_case_ids(self):
        with pytest.raises(bm.SuiteError, match="duplicate"):
            bm.load_suite({"tier": "lem-simple", "cases": [_case("a"), _case("a")]})

    def test_rejects_case_without_messages_or_assertions(self):
        with pytest.raises(bm.SuiteError):
            bm.load_suite({"tier": "lem-simple", "cases": [{"id": "a", "assertions": []}]})

    def test_missing_explicitly_requested_tier_raises(self, tmp_path):
        with pytest.raises(bm.SuiteError):
            bm.load_suites(str(tmp_path), ["lem-simple"])

    def test_thresholds_are_clamped_and_defaulted(self):
        assert bm.normalize_thresholds(None)["deterministic_pass_rate"] == 0.9
        assert bm.normalize_thresholds(None)["contract_pass_rate"] == 0.9
        assert bm.normalize_thresholds({"deterministic_pass_rate": 5})[
            "deterministic_pass_rate"] == 1.0
        assert bm.normalize_thresholds({"contract_pass_rate": 5})["contract_pass_rate"] == 1.0
        assert bm.normalize_thresholds({"judge_pass_rate": "x"})["judge_pass_rate"] == 0.8
        assert bm.normalize_thresholds({"min_judged": "x"})["min_judged"] == 3

    def test_rejects_an_unknown_production_class(self):
        with pytest.raises(bm.SuiteError, match="production class"):
            bm.load_suite({"tier": "lem-simple", "cases": [_case(assertions=[
                {"type": "max_chars", "value": 10, "production": "sometimes"}])]})

    def test_rejects_a_non_boolean_production_budget_flag(self):
        with pytest.raises(bm.SuiteError, match="budget_mirrors_production"):
            bm.load_suite({"tier": "lem-simple",
                           "cases": [_case(budget_mirrors_production="yes")]})


# ────────────────────────── deterministic assertions ─────────────────────────────

class TestAssertions:
    def test_max_and_min_chars(self):
        case = _case(assertions=[{"type": "max_chars", "value": 5}])
        assert bm.evaluate_case(case, "abc")["passes"] is True
        assert bm.evaluate_case(case, "abcdefg")["passes"] is False
        case = _case(assertions=[{"type": "min_chars", "value": 5}])
        assert bm.evaluate_case(case, "abc")["passes"] is False

    def test_no_preamble_catches_narration_only(self):
        case = _case(assertions=[{"type": "no_preamble"}])
        assert bm.evaluate_case(case, "Sure! Here you go.")["passes"] is False
        assert bm.evaluate_case(case, "Here is the list: a, b")["passes"] is False
        assert bm.evaluate_case(case, "Logistics, Retail")["passes"] is True

    def test_comma_list_counts_items_and_rejects_multiline(self):
        case = _case(assertions=[{"type": "comma_list", "min_items": 3, "max_items": 3}])
        assert bm.evaluate_case(case, "a, b, c")["passes"] is True
        assert bm.evaluate_case(case, "a, b")["passes"] is False
        assert bm.evaluate_case(case, "a, b, c\n- d")["passes"] is False

    def test_json_object_requires_bare_json_with_keys(self):
        case = _case(assertions=[{"type": "json_object", "required_keys": ["score"]}])
        assert bm.evaluate_case(case, '{"score": 1}')["passes"] is True
        assert bm.evaluate_case(case, '{"other": 1}')["passes"] is False
        assert bm.evaluate_case(case, '```json\n{"score": 1}\n```')["passes"] is False
        assert bm.evaluate_case(case, '[1, 2]')["passes"] is False

    def test_max_lines(self):
        case = _case(assertions=[{"type": "max_lines", "value": 1}])
        assert bm.evaluate_case(case, "one line")["passes"] is True
        assert bm.evaluate_case(case, "one\ntwo")["passes"] is False

    def test_required_and_forbidden_phrases(self):
        case = _case(assertions=[{"type": "required_phrases", "values": ["40", "days"],
                                  "mode": "all"}])
        assert bm.evaluate_case(case, "40 days")["passes"] is True
        assert bm.evaluate_case(case, "40 hours")["passes"] is False
        case = _case(assertions=[{"type": "required_phrases", "values": ["yes", "no"]}])
        assert bm.evaluate_case(case, "no")["passes"] is True
        case = _case(assertions=[{"type": "forbidden_phrases", "values": ["book a call"]}])
        assert bm.evaluate_case(case, "Want to book a call?")["passes"] is False

    def test_slop_lint_blocks_a_hard_violation(self):
        case = _case(assertions=[{"type": "slop_lint", "content_type": "post"}])
        slop = ("It's not about the tooling, it's about the people. "
                "Here's the kicker: nobody noticed.")
        assert bm.evaluate_case(case, slop)["passes"] is False

    def test_slop_lint_disabled_surface_is_unscored_not_a_pass(self, monkeypatch):
        monkeypatch.setenv("SLOP_LINT_ENABLED", "false")
        case = _case(assertions=[{"type": "slop_lint", "content_type": "post"}])
        result = bm.evaluate_case(case, "anything at all")
        assert result["passes"] is True          # nothing FAILED …
        assert result["unscored"] == 1           # … but nothing was measured either

    def test_comment_contract_uses_the_cases_target_post(self):
        case = _case(assertions=[{"type": "comment_contract"}],
                     context={"post_content": "We deleted our staging environment and put every "
                                              "change behind a feature flag."})
        good = ("Deleting staging only works because the flag expires. In my experience flags that "
                "outlive the change become a second configuration surface. How do you enforce it?")
        assert bm.evaluate_case(case, good)["passes"] is True
        assert bm.evaluate_case(case, "Great post! Thanks for sharing.")["passes"] is False

    def test_router_tier_grades_the_deterministic_router_contract(self):
        case = {"id": "r", "messages": [{"role": "user", "content": "Refine this line please"}],
                "assertions": [{"type": "router_tier", "value": "lem-simple"}]}
        assert bm.evaluate_case(case, "ok")["passes"] is True
        case["assertions"] = [{"type": "router_tier", "value": "lem-complex"}]
        assert bm.evaluate_case(case, "ok")["passes"] is False

    def test_max_similarity_compares_against_named_peer_cases(self):
        case = _case(assertions=[{"type": "max_similarity", "value": 0.3, "against": ["peer"]}])
        peers = {"peer": "the quick brown fox jumps over the lazy dog"}
        assert bm.evaluate_case(case, "an entirely different sentence about ships",
                                peers)["passes"] is True
        assert bm.evaluate_case(case, "the quick brown fox jumps over the lazy dog",
                                peers)["passes"] is False

    def test_max_similarity_without_a_peer_is_unscored(self):
        case = _case(assertions=[{"type": "max_similarity", "value": 0.3, "against": ["missing"]}])
        result = bm.evaluate_case(case, "text", {})
        assert result["passes"] is True and result["unscored"] == 1

    def test_min_burstiness_is_unscored_on_a_one_sentence_answer(self):
        case = _case(assertions=[{"type": "min_burstiness", "value": 0.2}])
        assert bm.evaluate_case(case, "short")["unscored"] == 1

    def test_missing_output_is_a_failure_not_a_dropped_case(self):
        result = bm.evaluate_case(_case(), None)
        assert result["passes"] is False
        assert result["failures"] == ["no output from the model"]

    def test_a_raising_assertion_fails_that_case_only(self):
        case = _case(assertions=[{"type": "max_chars", "value": "not-a-number"},
                                 {"type": "no_preamble"}])
        result = bm.evaluate_case(case, "clean answer")
        assert result["passes"] is False
        assert any("assertion raised" in f for f in result["failures"])
        assert result["assertions"][1]["passes"] is True


# ─────────── what production does with a failure — the #910 calibration ──────────

class TestProductionClass:
    def test_the_gates_production_regenerates_against_default_to_repairable(self):
        for kind in ("slop_lint", "comment_contract", "max_similarity", "min_burstiness"):
            assert bm.assertion_production_class({"type": kind}) == bm.PRODUCTION_REPAIRABLE

    def test_everything_else_defaults_to_contract(self):
        for kind in ("max_chars", "min_chars", "json_object", "no_preamble", "comma_list",
                     "router_tier", "required_phrases", "max_lines"):
            assert bm.assertion_production_class({"type": kind}) == bm.PRODUCTION_CONTRACT

    def test_an_unclassified_type_is_contract_not_silently_uncounted(self):
        assert bm.assertion_production_class({"type": "brand-new"}) == bm.PRODUCTION_CONTRACT
        assert bm.assertion_production_class(None) == bm.PRODUCTION_CONTRACT

    def test_a_fixture_may_override_per_assertion(self):
        # Repairable-ness is a property of the CALL SITE, not of the check: a craft-target max_chars
        # on long-form is redrafted, the same assertion carrying LinkedIn's hard limit is not.
        assert bm.assertion_production_class(
            {"type": "max_chars", "production": "repairable"}) == bm.PRODUCTION_REPAIRABLE
        assert bm.assertion_production_class(
            {"type": "slop_lint", "production": "contract"}) == bm.PRODUCTION_CONTRACT

    def test_a_repairable_only_failure_still_passes_the_contract(self):
        case = _case(assertions=[{"type": "max_chars", "value": 500},
                                 {"type": "slop_lint", "content_type": "comment"}])
        # The contrastive frame is exactly what `_gated_comment` retries against (#617/#625).
        result = bm.evaluate_case(
            case, "It isn't about the tooling; it's about the queue. We measured both for a month.")
        assert result["passes"] is False          # the first draft did not clear every check
        assert result["contract_passes"] is True  # but production regenerates rather than ships it
        assert result["repairable_failures"] and not result["contract_failures"]

    def test_a_contract_failure_fails_both(self):
        case = _case(assertions=[{"type": "max_chars", "value": 5}])
        result = bm.evaluate_case(case, "far too long to fit")
        assert result["passes"] is False and result["contract_passes"] is False
        assert result["contract_failures"] and not result["repairable_failures"]

    def test_no_output_is_a_contract_failure_there_is_no_repair_for_nothing(self):
        result = bm.evaluate_case(_case(), None)
        assert result["contract_passes"] is False
        assert result["contract_failures"] == ["no output from the model"]

    def test_the_shipped_long_form_suites_hold_linkedins_hard_limit_as_a_contract_check(self):
        """A craft cap under 3000 chars is a target production redrafts toward; 3000 is LinkedIn's
        own limit and nothing repairs a post that exceeds it."""
        suite = bm.load_suites(str(SUITE_DIR), ["lem-complex"])["lem-complex"]
        for case in suite["cases"]:
            caps = [a for a in case["assertions"] if a["type"] == "max_chars"]
            repairable = [a for a in caps
                          if bm.assertion_production_class(a) == bm.PRODUCTION_REPAIRABLE]
            if not repairable:
                continue
            hard = [a for a in caps
                    if bm.assertion_production_class(a) == bm.PRODUCTION_CONTRACT]
            assert hard, f"{case['id']}: a repairable cap with no hard limit behind it"
            assert max(a["value"] for a in hard) == 3000, case["id"]

    def test_every_production_budget_case_is_a_short_one(self):
        """The exemption exists for budgets that MIRROR a call site (`max_tokens` 3/5/8). Marking a
        long-form case would quietly re-disable the reasoning-headroom retry the #842 run needed."""
        for tier, suite in bm.load_suites(str(SUITE_DIR)).items():
            for case in suite["cases"]:
                if case.get("budget_mirrors_production"):
                    assert case["params"]["max_tokens"] <= 10, f"{tier}/{case['id']}"


# ───────────────────────────── scorecard merge ───────────────────────────────────

class TestScorecard:
    def test_rates_errors_and_latency_percentiles(self):
        results = [{"case_id": "a", "passes": True, "failures": [], "unscored": 0},
                   {"case_id": "b", "passes": False, "failures": ["max_chars: too long"],
                    "unscored": 0},
                   {"case_id": "c", "passes": False, "failures": [], "unscored": 0,
                    "error": "timeout"}]
        judge = {"a": {"passes": True, "status": "scored"},
                 "b": {"passes": False, "status": "scored", "reasoning": "generic filler"}}
        timings = {"a": {"latency_ms": 100, "total_tokens": 10},
                   "b": {"latency_ms": 300, "total_tokens": 20}, "c": {}}
        card = bm.merge_scorecard("lem-simple", "m", "candidate", results, judge, timings)
        assert card["deterministic_pass_rate"] == pytest.approx(1 / 3, abs=1e-4)
        assert card["errors"] == ["c"]
        assert card["judged"] == 2 and card["judge_pass_rate"] == 0.5
        assert card["total_tokens"] == 30
        assert card["latency_p50_ms"] == 100.0
        assert card["judge_notes"][0]["case_id"] == "b"

    def test_judge_timeouts_are_excluded_from_the_denominator(self):
        results = [{"case_id": "a", "passes": True, "failures": [], "unscored": 0},
                   {"case_id": "b", "passes": True, "failures": [], "unscored": 0}]
        judge = {"a": {"passes": True, "status": "scored"},
                 "b": {"passes": None, "status": bm.JUDGE_TIMEOUT}}
        card = bm.merge_scorecard("lem-simple", "m", "candidate", results, judge)
        assert card["judged"] == 1
        assert card["judge_timeouts"] == 1
        assert card["judge_pass_rate"] == 1.0

    def test_no_judge_verdicts_leave_the_rate_unknown_not_zero(self):
        results = [{"case_id": "a", "passes": True, "failures": [], "unscored": 0}]
        card = bm.merge_scorecard("lem-simple", "m", "candidate", results, {})
        assert card["judge_pass_rate"] is None

    def test_both_rates_are_reported_and_the_redraft_cost_is_named(self):
        results = [{"case_id": "a", "passes": True, "contract_passes": True, "unscored": 0},
                   {"case_id": "b", "passes": False, "contract_passes": True, "unscored": 0,
                    "failures": ["slop_lint: contrastive frame"],
                    "repairable_failures": ["slop_lint: contrastive frame"],
                    "contract_failures": []},
                   {"case_id": "c", "passes": False, "contract_passes": False, "unscored": 0,
                    "failures": ["max_chars: 3915 chars > 3000"], "repairable_failures": [],
                    "contract_failures": ["max_chars: 3915 chars > 3000"]}]
        card = bm.merge_scorecard("lem-complex", "m", "candidate", results)
        assert card["deterministic_pass_rate"] == pytest.approx(1 / 3, abs=1e-4)
        assert card["contract_pass_rate"] == pytest.approx(2 / 3, abs=1e-4)
        assert card["repaired_cases"] == ["b"]
        assert card["failed_cases"][0]["repairable_failures"] == ["slop_lint: contrastive frame"]

    def test_cases_that_were_re_measured_or_budget_locked_are_named(self):
        results = [{"case_id": "a", "passes": True, "contract_passes": True, "unscored": 0},
                   {"case_id": "b", "passes": True, "contract_passes": True, "unscored": 0}]
        timings = {"a": {"repeats": 1}, "b": {"budget_locked": True}}
        card = bm.merge_scorecard("lem-simple", "m", "candidate", results, {}, timings)
        assert card["remeasured_cases"] == ["a"]
        assert card["budget_locked_cases"] == ["b"]

    def test_cases_that_needed_reasoning_headroom_are_named_not_silently_absorbed(self):
        results = [{"case_id": "a", "passes": True, "failures": [], "unscored": 0},
                   {"case_id": "b", "passes": True, "failures": [], "unscored": 0}]
        timings = {"a": {"latency_ms": 1, "budget_escalations": 2},
                   "b": {"latency_ms": 1, "budget_escalations": 0}}
        card = bm.merge_scorecard("lem-complex", "minimax-m3", "candidate", results, {}, timings)
        assert card["budget_escalated_cases"] == ["a"]


# ─────────────────────────── champion/challenger gate ────────────────────────────

class TestGate:
    def test_recommends_when_floors_are_met_and_champion_is_matched(self):
        gate = bm.gate_decision(_card(det=0.95), _card(role="champion", det=0.9),
                                {"deterministic_pass_rate": 0.9})
        assert gate["verdict"] == bm.VERDICT_RECOMMEND
        assert gate["blockers"] == []

    def test_rejects_a_candidate_below_the_absolute_floor(self):
        gate = bm.gate_decision(_card(det=0.5), _card(role="champion", det=0.4),
                                {"contract_pass_rate": 0.9})
        assert gate["verdict"] == bm.VERDICT_REJECT
        assert "contract floor" in gate["blockers"]

    def test_rejects_a_candidate_that_loses_to_the_champion(self):
        gate = bm.gate_decision(_card(det=0.92), _card(role="champion", det=0.99),
                                {"contract_pass_rate": 0.9})
        assert gate["verdict"] == bm.VERDICT_REJECT
        assert "beats champion (first-draft)" in gate["blockers"]

    def test_a_tie_counts_as_meets_or_beats(self):
        gate = bm.gate_decision(_card(det=0.9), _card(role="champion", det=0.9),
                                {"deterministic_pass_rate": 0.9})
        assert gate["verdict"] == bm.VERDICT_RECOMMEND

    def test_losing_on_judge_alone_still_rejects(self):
        gate = bm.gate_decision(_card(judge_rate=0.8), _card(role="champion", judge_rate=1.0),
                                {"judge_pass_rate": 0.8})
        assert gate["verdict"] == bm.VERDICT_REJECT
        assert "beats champion (judge)" in gate["blockers"]

    def test_provider_errors_block_a_recommendation(self):
        gate = bm.gate_decision(_card(errors=["c1"]), _card(role="champion"), {})
        assert gate["verdict"] == bm.VERDICT_REJECT
        assert "provider answered every case" in gate["blockers"]

    def test_no_champion_is_no_baseline_never_a_pass(self):
        gate = bm.gate_decision(_card(), None, {})
        assert gate["verdict"] == bm.VERDICT_NO_BASELINE

    def test_thin_judge_evidence_degrades_to_deterministic_only(self):
        gate = bm.gate_decision(_card(judged=1, judge_rate=1.0),
                                _card(role="champion", judged=1, judge_rate=1.0),
                                {"min_judged": 3})
        assert gate["verdict"] == bm.VERDICT_DETERMINISTIC_ONLY
        assert bm.swap_recommendations([gate]) == []

    def test_only_full_recommendations_become_swap_recommendations(self):
        gates = [
            {"tier": "lem-simple", "model": "a", "champion": "old", "verdict": bm.VERDICT_RECOMMEND},
            {"tier": "lem-medium", "model": "b", "champion": "old2", "verdict": bm.VERDICT_REJECT},
            {"tier": "lem-complex", "model": "c", "champion": None,
             "verdict": bm.VERDICT_NO_BASELINE},
        ]
        recs = bm.swap_recommendations(gates)
        assert [{k: r[k] for k in ("tier", "model", "champion")} for r in recs] == [
            {"tier": "lem-simple", "model": "a", "champion": "old"}]
        # A gate with no delta recorded still ships one, so a consumer never reads a missing key
        # as "flat" (issue #842).
        assert recs[0]["usage_delta"]["direction"] == bm.USAGE_UNKNOWN

    def test_gate_run_pairs_each_candidate_with_its_own_tier_champion(self):
        cards = [_card(tier="lem-simple", model="champ-s", role="champion"),
                 _card(tier="lem-medium", model="champ-m", role="champion"),
                 _card(tier="lem-medium", model="cand")]
        gates = bm.gate_run(cards, {"lem-medium": _suite("lem-medium")})
        assert len(gates) == 1 and gates[0]["champion"] == "champ-m"

    def test_the_first_draft_rate_is_advisory_not_a_floor(self):
        """The #910 finding: every model measured, champions included, sat at 40–80% first-draft
        against a 90% floor. A floor the incumbent fails is a gate that can never open."""
        gate = bm.gate_decision(_card(det=0.4, contract=1.0),
                                _card(role="champion", det=0.4, contract=1.0),
                                {"contract_pass_rate": 0.9, "deterministic_pass_rate": 0.9})
        assert gate["verdict"] == bm.VERDICT_RECOMMEND
        advisory = gate["advisories"][0]
        assert advisory["expectation"] == "first-draft yield" and advisory["passes"] is False
        assert [e["expectation"] for e in gate["expectations"] if e["passes"] is False] == []

    def test_a_candidate_that_ships_more_broken_output_is_rejected(self):
        gate = bm.gate_decision(_card(det=1.0, contract=0.8), _card(role="champion", contract=1.0),
                                {"contract_pass_rate": 0.9})
        assert gate["verdict"] == bm.VERDICT_REJECT
        assert "contract floor" in gate["blockers"]

    def test_a_candidate_below_its_champions_contract_rate_is_rejected(self):
        gate = bm.gate_decision(_card(contract=0.9), _card(role="champion", contract=1.0),
                                {"contract_pass_rate": 0.9})
        assert "beats champion (contract)" in gate["blockers"]

    def test_a_champion_that_misses_the_floor_is_recorded_on_every_verdict(self):
        gate = bm.gate_decision(_card(contract=1.0), _card(role="champion", contract=0.7),
                                {"contract_pass_rate": 0.9})
        assert gate["champion_clears_contract_floor"] is False
        assert bm.gate_decision(_card(), _card(role="champion"),
                                {})["champion_clears_contract_floor"] is True
        assert bm.gate_decision(_card(), None, {})["champion_clears_contract_floor"] is None

    def test_mapping_lines_are_rendered_not_written(self):
        lines = bm.recommendation_mapping_lines(
            [{"tier": "lem-simple", "model": "new", "champion": "old"}])
        assert lines == ['  "old": "new"  # lem-simple: benchmark-recommended']


# ─────────────── the #842 roster, replayed under the #910 calibration ────────────
#
# Acceptance for #910: "a re-run of the #842 roster under the new calibration produces a verdict
# that could, in principle, be a `recommend` — i.e. the gate is demonstrably openable". A live
# re-run needs a metered key, so the demonstration is a REPLAY: every failing case from the
# committed report (`docs/model-benchmarks/2026-08-02-bm-20260802-20ae40.md`) is re-scored through
# the real classifier and the real gate. Nothing here is invented — each entry is one ❌ line of
# that report, with the fixture assertion it came from.

# (case failures) per model, verbatim from the report's per-expectation detail. `max_chars-hard` is
# a breach of LinkedIn's own 3000-char limit (contract); `max_chars-craft` is the tier's tighter
# dwell target, which production redrafts toward.
BM842 = {
    ("lem-complex", "qwen3.5:397b", "champion"): {
        "failures": [["max_chars-hard"], ["max_chars-hard", "slop_lint", "required_phrases"],
                     ["max_chars-craft"], ["slop_lint"], ["max_chars-hard"]],
        "judged": 5, "judge_rate": 1.0, "usage": {"level": 2, "label": "medium"}},
    ("lem-complex", "deepseek-v4-flash", "candidate"): {
        "failures": [["slop_lint"], ["required_phrases"], ["slop_lint"]],
        "judged": 7, "judge_rate": 0.57, "usage": {"level": 2, "label": "medium"}},
    ("lem-complex", "gemma4:31b", "candidate"): {
        "failures": [["slop_lint"], ["slop_lint"]],
        "judged": 7, "judge_rate": 0.57, "usage": {"level": 1, "label": "low"}},
    ("lem-complex", "minimax-m3", "candidate"): {
        "failures": [["min_chars"], ["slop_lint", "required_phrases"], ["slop_lint"],
                     ["slop_lint"], ["required_phrases"], ["slop_lint"]],
        "judged": 4, "judge_rate": 0.75, "usage": {"level": 3, "label": "high"}},
    ("lem-complex", "glm-5.2", "candidate"): {
        "failures": [["min_chars"], ["min_chars"], ["min_chars"]],
        "judged": 7, "judge_rate": 0.86, "usage": {"level": 3, "label": "high"}},
    ("lem-medium", "gpt-oss:120b", "champion"): {
        "failures": [["comment_contract"]] * 4,
        "judged": 6, "judge_rate": 0.5, "usage": {"level": 2, "label": "medium"}},
    ("lem-medium", "deepseek-v4-flash", "candidate"): {
        "failures": [["comment_contract"]] * 4,
        "judged": 6, "judge_rate": 0.83, "usage": {"level": 2, "label": "medium"}},
    ("lem-medium", "gemma4:31b", "candidate"): {
        "failures": [["comment_contract"]] * 5 + [["required_phrases"]],
        "judged": 4, "judge_rate": 1.0, "usage": {"level": 1, "label": "low"}},
    ("lem-medium", "minimax-m3", "candidate"): {
        "failures": [["comment_contract"]] * 5,
        "judged": 5, "judge_rate": 0.8, "usage": {"level": 3, "label": "high"}},
    ("lem-medium", "glm-5.2", "candidate"): {
        "failures": [["comment_contract"]] * 5 + [["min_chars"]],
        "judged": 4, "judge_rate": 0.75, "usage": {"level": 3, "label": "high"}},
}

# How each measured failure maps onto a fixture assertion, so the REAL classifier decides whether
# production would have shipped it.
BM842_SPECS = {
    "max_chars-hard": {"type": "max_chars", "value": 3000, "production": "contract"},
    "max_chars-craft": {"type": "max_chars", "value": 2400, "production": "repairable"},
    "min_chars": {"type": "min_chars", "value": 700},
    "slop_lint": {"type": "slop_lint"},
    "comment_contract": {"type": "comment_contract"},
    "required_phrases": {"type": "required_phrases"},
}


def _replay_cards(cases: int = 10) -> list:
    cards = []
    for (tier, model, role), measured in BM842.items():
        results = []
        for index in range(cases):
            failed = measured["failures"][index] if index < len(measured["failures"]) else []
            assertions = [{"type": BM842_SPECS[k]["type"],
                           "production": bm.assertion_production_class(BM842_SPECS[k]),
                           "passes": False, "detail": k} for k in failed]
            contract = [f"{a['type']}: {a['detail']}" for a in assertions
                        if a["production"] == bm.PRODUCTION_CONTRACT]
            repairable = [f"{a['type']}: {a['detail']}" for a in assertions
                          if a["production"] == bm.PRODUCTION_REPAIRABLE]
            results.append({"case_id": f"{tier}-{index}", "passes": not failed,
                            "contract_passes": not contract, "assertions": assertions,
                            "failures": contract + repairable, "contract_failures": contract,
                            "repairable_failures": repairable, "unscored": 0})
        judged = measured["judged"]
        judge = {r["case_id"]: {"passes": i < round(judged * measured["judge_rate"]),
                                "status": "scored"}
                 for i, r in enumerate(results[:judged])}
        card = bm.merge_scorecard(tier, model, role, results, judge)
        card["usage"] = measured["usage"]
        card["benchmark_run_id"] = "bm-20260802-20ae40"
        cards.append(card)
    return cards


class TestBm842Replay:
    def _gates(self):
        suites = bm.load_suites(str(SUITE_DIR), ["lem-complex", "lem-medium"])
        return {(g["tier"], g["model"]): g for g in bm.gate_run(_replay_cards(), suites)}

    def test_the_old_absolute_floor_could_not_have_opened_for_anyone(self):
        """Why the calibration changed: not one model in that roster — champions included — was
        within reach of the 90% floor on the first-draft rate."""
        for card in _replay_cards():
            assert card["deterministic_pass_rate"] < 0.9, card["model"]

    def test_the_gate_is_openable_deepseek_now_earns_a_recommend_on_lem_medium(self):
        gate = self._gates()[("lem-medium", "deepseek-v4-flash")]
        assert gate["verdict"] == bm.VERDICT_RECOMMEND, gate["expectations"]
        assert bm.swap_recommendations([gate])[0]["champion"] == "gpt-oss:120b"
        # Flat on quota, so the standing #842 spend policy takes it without the owner.
        assert gate["quota_policy"]["decision"] == bm.POLICY_ADOPT

    def test_it_is_the_only_recommendation_the_rest_still_fail_on_real_quality(self):
        gates = self._gates()
        recommended = [k for k, g in gates.items() if g["verdict"] == bm.VERDICT_RECOMMEND]
        assert recommended == [("lem-medium", "deepseek-v4-flash")]
        # …and each rejection now names a quality reason, not the floor everybody failed.
        assert "judge floor" in gates[("lem-complex", "gemma4:31b")]["blockers"]
        assert "contract floor" in gates[("lem-complex", "glm-5.2")]["blockers"]
        assert "beats champion (first-draft)" in gates[("lem-medium", "minimax-m3")]["blockers"]

    def test_the_champions_contract_rates_are_measured_not_assumed(self):
        cards = {(c["tier"], c["model"]): c for c in _replay_cards() if c["role"] == "champion"}
        # Every lem-medium failure was the #617 comment contract, which production retries and then
        # SKIPS the post over — nothing broken ships, so the incumbent clears its own floor.
        assert cards[("lem-medium", "gpt-oss:120b")]["contract_pass_rate"] == 1.0
        # lem-complex is the honest opposite: three drafts over LinkedIn's own 3000-char limit is a
        # real defect, and the floor is supposed to show it.
        assert cards[("lem-complex", "qwen3.5:397b")]["contract_pass_rate"] == 0.7

    def test_the_report_says_when_the_incumbent_misses_its_own_floor(self):
        gates = list(self._gates().values())
        run = {"source": bm.BENCHMARK_SOURCE, "run_id": "bm-20260802-20ae40",
               "date": "2026-08-02", "scoring_mode": bm.SCORING_FALLBACK,
               "tiers": ["lem-complex", "lem-medium"], "candidates": ["deepseek-v4-flash"],
               "scorecards": _replay_cards(), "gates": gates,
               "recommendations": bm.swap_recommendations(gates)}
        text = bm.render_report(run)
        assert "misses this tier's own\ncontract floor" in text or \
               "misses this tier's own contract floor" in text
        assert "first-draft yield (advisory)" in text


# ───────────────────────────── provenance guard ──────────────────────────────────

class TestProvenance:
    def _run(self, **over):
        run = {"source": bm.BENCHMARK_SOURCE, "run_id": "bm-1", "date": "2026-07-27",
               "scoring_mode": bm.SCORING_DRY_RUN, "tiers": ["lem-simple"], "candidates": [],
               "judge_calls": 0, "judge_call_cap": 10,
               "scorecards": [_card()], "gates": [], "recommendations": []}
        run["scorecards"][0]["benchmark_run_id"] = "bm-1"
        run.update(over)
        return run

    def test_accepts_a_well_formed_benchmark_run(self):
        bm.assert_benchmark_only(self._run())

    def test_refuses_a_non_benchmark_source(self):
        with pytest.raises(ValueError, match="non-benchmark source"):
            bm.assert_benchmark_only(self._run(source="production"))

    def test_refuses_a_scorecard_from_another_run(self):
        run = self._run()
        run["scorecards"][0]["benchmark_run_id"] = "bm-other"
        with pytest.raises(ValueError, match="not tagged with this run_id"):
            bm.assert_benchmark_only(run)

    def test_refuses_a_scorecard_carrying_a_person(self):
        run = self._run()
        run["scorecards"][0]["user_id"] = 7
        with pytest.raises(ValueError, match="anonymous"):
            bm.assert_benchmark_only(run)

    def test_render_report_refuses_a_tainted_run(self):
        with pytest.raises(ValueError):
            bm.render_report(self._run(source="prod-traffic"))

    def test_write_report_never_touches_disk_for_a_tainted_run(self, tmp_path):
        out = tmp_path / "reports"
        with pytest.raises(ValueError):
            bm.write_report(self._run(source="prod-traffic"), str(out))
        assert not out.exists()


# ───────────────────────── harness-outage guard (#923) ───────────────────────────

def _measured_card(tier="lem-simple", model="m", role="candidate", cases=("a", "b"),
                   errors=None, error="No module named 'openai'", run_id="bm-1"):
    """A real scorecard built through the real merge, so the guard is exercised against the shape
    `run_benchmark` actually produces. `errors` names the cases whose provider call failed."""
    failed = set(cases) if errors is None else set(errors)
    results = []
    for case_id in cases:
        case = {"id": case_id, "assertions": [{"type": "max_chars", "value": 100}]}
        results.append(bm.evaluate_case(case, None) | {"error": error} if case_id in failed
                       else bm.evaluate_case(case, "ok"))
    card = bm.merge_scorecard(tier, model, role, results)
    card["benchmark_run_id"] = run_id
    return card


def _outage_run(cards, **over):
    run = {"source": bm.BENCHMARK_SOURCE, "run_id": "bm-1", "date": "2026-08-02",
           "scoring_mode": bm.SCORING_DETERMINISTIC, "tiers": ["lem-simple"],
           "candidates": ["cand"], "judge_calls": 0, "judge_call_cap": 0,
           "scorecards": list(cards), "gates": [], "recommendations": []}
    run.update(over)
    return run


class TestHarnessOutage:
    """#923 — a harness-wide failure is an outage, not a scorecard of zeros.

    `ProviderClient.complete` turns any exception into a case result, so a broken venv or a revoked
    key produced a complete, plausible report: six rows, `reject` everywhere, champions at 0%, off a
    run that never made an HTTP request. Nothing measured => nothing published."""

    def test_every_case_of_every_model_erroring_is_an_outage(self):
        run = _outage_run([_measured_card(model="champ", role="champion"),
                           _measured_card(model="cand")])
        outage = bm.harness_outage(run)
        assert outage is not None
        assert outage["cases"] == 4 and outage["scorecards"] == 2
        assert outage["error"] == "No module named 'openai'"

    def test_a_champion_at_zero_is_the_tell_and_is_named(self):
        """A roster of candidates can be bad; the model production already runs cannot be."""
        run = _outage_run([_measured_card(model="gpt-oss:120b", role="champion"),
                           _measured_card(model="cand")])
        outage = bm.harness_outage(run)
        assert outage["champions"] == ["gpt-oss:120b"]
        assert "plumbing and not the roster" in outage["detail"]

    def test_the_underlying_error_is_named_once_not_per_case(self):
        run = _outage_run([_measured_card(model="champ", role="champion", cases=tuple("abcde")),
                           _measured_card(model="cand", cases=tuple("abcde"))])
        detail = bm.harness_outage(run)["detail"]
        assert detail.count("No module named 'openai'") == 1

    def test_a_second_distinct_error_is_counted_not_listed(self):
        run = _outage_run([_measured_card(model="champ", role="champion", cases=("a", "b", "c")),
                           _measured_card(model="cand", cases=("a",), error="Connection error.")])
        outage = bm.harness_outage(run)
        assert outage["distinct_errors"] == 2
        # The commonest message is the cause; the rest are a count, not a wall of text.
        assert outage["error"] == "No module named 'openai'"
        assert "+1 other distinct error(s)" in outage["detail"]

    def test_one_model_failing_every_case_is_still_a_measurement(self):
        """The honest half: a candidate that 500s on everything beside a champion that answered is
        a real result and must render exactly as it does today."""
        run = _outage_run([_measured_card(model="champ", role="champion", errors=()),
                           _measured_card(model="cand", error="410 model retired")])
        assert bm.harness_outage(run) is None
        assert bm.render_report(run)
        assert len(bm.leaderboard_rows(run)) == 2

    def test_some_cases_failing_everywhere_is_still_a_measurement(self):
        run = _outage_run([_measured_card(model="champ", role="champion", errors=("a",)),
                           _measured_card(model="cand", errors=("a",))])
        assert bm.harness_outage(run) is None

    def test_a_run_with_no_scored_cases_is_not_flagged(self):
        assert bm.harness_outage(_outage_run([])) is None
        assert bm.harness_outage({}) is None

    def test_a_dry_run_of_canned_outputs_is_never_an_outage(self):
        run = bm.run_benchmark({"lem-simple": _suite("lem-simple", cases=[
            _case("a", assertions=[{"type": "max_chars", "value": 5}],
                  canned={"output": "far too long to fit", "judge_pass": False})],
            thresholds={"min_judged": 1})}, ["cand"], {"lem-simple": "champ"},
            run_id="bm-1", today="2026-08-02", dry_run=True, judge_cap=2)
        assert run["harness_outage"] is None and bm.render_report(run)

    def test_render_report_refuses_a_run_that_measured_nothing(self):
        run = _outage_run([_measured_card(model="champ", role="champion"),
                           _measured_card(model="cand")])
        with pytest.raises(ValueError, match="harness outage"):
            bm.render_report(run)

    def test_write_report_writes_no_report_and_no_leaderboard_rows(self, tmp_path):
        out = tmp_path / "docs"
        out.mkdir()
        readme = out / "README.md"
        before = f"# Benchmarks\n\n{bm.LEADERBOARD_BEGIN}\n{bm.LEADERBOARD_END}\n"
        readme.write_text(before)
        run = _outage_run([_measured_card(model="champ", role="champion"),
                           _measured_card(model="cand")])
        with pytest.raises(ValueError, match="harness outage"):
            bm.write_report(run, str(out))
        assert readme.read_text() == before
        assert list(out.iterdir()) == [readme]

    def test_run_benchmark_records_the_outage_on_the_results_document(self):
        provider = MagicMock()
        provider.complete.return_value = {"text": None, "error": "No module named 'openai'",
                                          "latency_ms": None, "usage": {}}
        run = bm.run_benchmark({"lem-simple": _suite("lem-simple", cases=[_case("a"), _case("b")],
                                                     thresholds={"min_judged": 1})},
                               ["cand"], {"lem-simple": "champ"}, run_id="bm-1",
                               today="2026-08-02", provider=provider, judge_cap=0,
                               judge_enabled=False)
        assert run["harness_outage"]["error"] == "No module named 'openai'"
        assert run["harness_outage"]["champions"] == ["champ"]

    def test_a_run_with_no_provider_configured_never_publishes(self):
        """The #921 shape: nothing was configured, so nothing was called, so nothing measured."""
        run = bm.run_benchmark({"lem-simple": _suite("lem-simple", cases=[_case("a")],
                                                     thresholds={"min_judged": 1})},
                               ["cand"], {"lem-simple": "champ"}, run_id="bm-1",
                               today="2026-08-02", provider=None, judge_cap=0, judge_enabled=False)
        assert "no provider client configured" in run["harness_outage"]["error"]
        with pytest.raises(ValueError, match="harness outage"):
            bm.render_report(run)


class TestUnmeasuredHeader:
    """The distinction belongs in the report HEADER, not only under the per-case ❌ details."""

    def test_the_header_counts_unmeasured_cases_and_names_a_silent_model(self):
        run = _outage_run([_measured_card(model="champ", role="champion", errors=()),
                           _measured_card(model="cand")])
        text = bm.render_report(run)
        assert "**Unmeasured cases:** 2 of 4" in text
        assert "`cand` answered nothing at all" in text
        assert "not a zero-quality result" in text

    def test_a_clean_run_carries_no_unmeasured_line(self):
        run = _outage_run([_measured_card(model="champ", role="champion", errors=()),
                           _measured_card(model="cand", errors=())])
        assert "Unmeasured cases" not in bm.render_report(run)


# ─────────────────────────── report + leaderboard ────────────────────────────────

class TestRendering:
    def _run(self):
        candidate = _card(model="new", det=1.0)
        champion = _card(model="old", role="champion", det=0.9)
        champion["failed_cases"] = [{"case_id": "c1", "failures": ["max_chars: 400 chars > 300"]}]
        champion["judge_notes"] = [{"case_id": "c1", "reasoning": "reads like a template"}]
        for card in (candidate, champion):
            card["benchmark_run_id"] = "bm-1"
        gates = [{"tier": "lem-simple", "model": "new", "champion": "old",
                  "verdict": bm.VERDICT_RECOMMEND,
                  "expectations": [{"expectation": "deterministic floor", "passes": True,
                                    "detail": "100% vs floor 90%"},
                                   {"expectation": "beats champion (judge)", "passes": None,
                                    "detail": "judge evidence missing on one side"}]}]
        return {"source": bm.BENCHMARK_SOURCE, "run_id": "bm-1", "date": "2026-07-27",
                "scoring_mode": bm.SCORING_POSTHOG, "tiers": ["lem-simple"], "candidates": ["new"],
                "judge_calls": 12, "judge_call_cap": 200,
                "scorecards": [candidate, champion], "gates": gates,
                "recommendations": bm.swap_recommendations(gates)}

    def test_report_contains_both_models_the_verdict_and_the_mapping_block(self):
        text = bm.render_report(self._run())
        assert "`new`" in text and "`old`" in text
        assert "**recommend**" in text
        assert 'max_chars: 400 chars > 300' in text
        assert "reads like a template" in text
        assert '"old": "new"' in text
        assert "➖" in text  # an unscored expectation is shown, not hidden

    def test_report_names_the_cases_that_needed_reasoning_headroom(self):
        run = self._run()
        run["scorecards"][0]["budget_escalated_cases"] = ["complex-personal-story"]
        text = bm.render_report(run)
        assert "reasoning headroom" in text and "`complex-personal-story`" in text

    def test_report_separates_what_production_ships_from_what_it_redrafts(self):
        run = self._run()
        run["scorecards"][1]["failed_cases"] = [
            {"case_id": "ships", "failures": ["max_chars: 3915 chars > 3000"],
             "contract_failures": ["max_chars: 3915 chars > 3000"], "repairable_failures": []},
            {"case_id": "redrafted", "failures": ["slop_lint: contrastive frame"],
             "contract_failures": [], "repairable_failures": ["slop_lint: contrastive frame"]}]
        text = bm.render_report(run)
        assert "❌ `ships` — production would ship this" in text
        assert "🔁 `redrafted` — first draft only" in text

    def test_a_case_that_produced_nothing_is_not_reported_as_shipped_output(self):
        """`no output` IS a contract failure — the call site got nothing — but production never
        SHIPPED it, and since #910 turns an empty completion into no output this is the common
        path. Rendering it under "production would ship this" is the misreading the two rates
        exist to prevent."""
        card = bm.merge_scorecard("lem-complex", "minimax-m3", "candidate", [bm.evaluate_case(
            {"id": "complex-thought-leadership", "assertions": [{"type": "min_chars", "value": 700},
                                                                {"type": "slop_lint"}]},
            None) | {"error": "empty completion after 2 budget escalation(s) and 1 "
                              "re-measurement(s)"}])
        run = self._run()
        run["scorecards"][1] = card | {"benchmark_run_id": run["run_id"], "usage": None}
        text = bm.render_report(run)
        assert "❌ `complex-thought-leadership` — no output; nothing was measured here" in text
        assert "empty completion after 2 budget escalation(s)" in text
        assert "production would ship this" not in text
        # …and it still fails the gate: no output is never a pass.
        assert card["contract_pass_rate"] == 0.0 and card["errors"] == [
            "complex-thought-leadership"]

    def test_report_names_re_measured_and_budget_locked_cases(self):
        run = self._run()
        run["scorecards"][0]["remeasured_cases"] = ["complex-thought-leadership"]
        run["scorecards"][0]["budget_locked_cases"] = ["simple-relevance-yes-no"]
        text = bm.render_report(run)
        assert "measurement variance" in text and "`complex-thought-leadership`" in text
        assert "production budget" in text and "`simple-relevance-yes-no`" in text

    def test_report_says_so_when_nothing_is_recommended(self):
        run = self._run()
        run["recommendations"] = []
        assert "None. No candidate met" in bm.render_report(run)

    def test_report_filename_is_date_and_run_id(self):
        assert bm.report_filename(self._run()) == "2026-07-27-bm-1.md"

    def test_leaderboard_rows_carry_the_gate_verdict_and_baseline_role(self):
        rows = bm.leaderboard_rows(self._run())
        by_model = {r["model"]: r for r in rows}
        assert by_model["new"]["verdict"] == bm.VERDICT_RECOMMEND
        assert by_model["old"]["verdict"] == "baseline"

    def test_update_leaderboard_creates_the_block_when_absent(self):
        text = bm.update_leaderboard("# Title\n", bm.leaderboard_rows(self._run()))
        assert bm.LEADERBOARD_BEGIN in text and bm.LEADERBOARD_END in text
        assert text.count("| lem-simple |") == 2

    def test_re_rendering_the_same_run_replaces_rather_than_duplicates(self):
        rows = bm.leaderboard_rows(self._run())
        once = bm.update_leaderboard("# Title\n", rows)
        twice = bm.update_leaderboard(once, rows)
        assert twice.count("| lem-simple |") == 2

    def test_a_later_run_is_prepended_and_older_rows_are_kept(self):
        first = bm.update_leaderboard("# Title\n", bm.leaderboard_rows(self._run()))
        later = self._run()
        later["run_id"] = "bm-2"
        later["date"] = "2026-08-03"
        for card in later["scorecards"]:
            card["benchmark_run_id"] = "bm-2"
        merged = bm.update_leaderboard(first, bm.leaderboard_rows(later))
        body = merged.split(bm.LEADERBOARD_BEGIN)[1]
        assert body.index("bm-2") < body.index("bm-1")
        assert merged.count("| lem-simple |") == 4

    def test_repeated_renders_never_grow_the_table_with_its_own_header(self):
        """The header and divider are pipe-delimited rows of the right width. Reading them back as
        DATA appended two junk rows per run and evicted real history at the row cap."""
        def table_rows(doc: str) -> list:
            inner = doc.split(bm.LEADERBOARD_BEGIN)[1].split(bm.LEADERBOARD_END)[0]
            return [line for line in inner.splitlines() if line.strip().startswith("|")]

        text = (_ROOT / "docs" / "model-benchmarks" / "README.md").read_text()
        # Measured against whatever history the committed table holds TODAY — a real run's rows
        # land in it, so a fixed expected count would fail on the next benchmark instead of on a
        # regression.
        committed = len(table_rows(text)) - 2
        for run_id in ("bm-1", "bm-2", "bm-3"):
            run = self._run()
            run["run_id"] = run_id
            for card in run["scorecards"]:
                card["benchmark_run_id"] = run_id
            text = bm.update_leaderboard(text, bm.leaderboard_rows(run))
        rows = table_rows(text)
        assert rows[0] == bm.LEADERBOARD_HEADER and rows[1] == bm.LEADERBOARD_DIVIDER
        # three runs × two scorecards on top of the committed history, and nothing else
        assert len(rows) == 2 + committed + 6
        assert bm.LEADERBOARD_HEADER not in rows[2:]

    def test_pre_910_rows_survive_the_new_contract_column(self):
        """Adding a column must not evict the history already committed: a legacy row is re-emitted
        at the new width with `n/a` where nobody measured a contract rate."""
        legacy = ("| 2026-08-02 | `bm-old` | lem-medium | `gpt-oss:120b` | champion | 60% | 50% | "
                  "1635 ms | baseline |")
        doc = f"{bm.LEADERBOARD_BEGIN}\n{bm.LEADERBOARD_HEADER}\n{bm.LEADERBOARD_DIVIDER}\n" \
              f"{legacy}\n{bm.LEADERBOARD_END}\n"
        merged = bm.update_leaderboard(doc, bm.leaderboard_rows(self._run()))
        kept = [line for line in merged.splitlines() if "bm-old" in line]
        assert len(kept) == 1
        cells = [c.strip() for c in kept[0].strip().strip("|").split("|")]
        assert len(cells) == len(bm.LEADERBOARD_COLUMNS)
        assert cells[5] == "n/a" and cells[6] == "60%"  # contract unknown, first draft preserved

    def test_the_committed_leaderboard_has_the_markers(self):
        text = (_ROOT / "docs" / "model-benchmarks" / "README.md").read_text()
        assert bm.LEADERBOARD_BEGIN in text and bm.LEADERBOARD_END in text


# ─────────────────────────────── judge plumbing ──────────────────────────────────

class TestJudge:
    def test_only_deterministically_passing_opted_in_cases_are_judged(self):
        suite = _suite(cases=[_case("a"), _case("b"), _case("c", judge=False)])
        results = [{"case_id": "a", "passes": True}, {"case_id": "b", "passes": False},
                   {"case_id": "c", "passes": True}]
        assert bm.judgeable_cases(results, suite) == ["a"]

    def test_a_case_with_a_provider_error_is_never_judged(self):
        suite = _suite(cases=[_case("a")])
        assert bm.judgeable_cases([{"case_id": "a", "passes": True, "error": "boom"}],
                                  suite) == []

    def test_the_cap_truncates_in_suite_order(self):
        suite = _suite(cases=[_case("a"), _case("b"), _case("c")])
        results = [{"case_id": c, "passes": True} for c in ("a", "b", "c")]
        assert bm.plan_judge_calls(results, suite, 2) == ["a", "b"]
        assert bm.plan_judge_calls(results, suite, 0) == []

    def test_judge_prompt_is_self_contained(self):
        suite = _suite()
        text = bm.judge_messages(suite, _case(), "the answer")[1]["content"]
        assert suite["contract"] in text and suite["judge_rubric"] in text
        assert "the answer" in text
        assert ".py" not in text  # a rubric that points at a repo path grades nothing

    def test_parse_judge_verdict_handles_bare_json_and_fenced_json(self):
        assert bm.parse_judge_verdict('{"pass": true, "reasoning": "fine"}')["passes"] is True
        fenced = '```json\n{"pass": false, "reasoning": "generic"}\n```'
        assert bm.parse_judge_verdict(fenced)["passes"] is False

    def test_parse_judge_verdict_finds_json_inside_prose(self):
        verdict = bm.parse_judge_verdict('Verdict: {"pass": false, "reasoning": "x"} done')
        assert verdict["passes"] is False

    def test_an_unparseable_verdict_is_unscored_not_a_pass(self):
        for reply in ("", "looks good to me", '{"verdict": "yes"}', None):
            verdict = bm.parse_judge_verdict(reply)
            assert verdict["passes"] is None
            assert verdict["status"] == bm.JUDGE_TIMEOUT

    def _judge_client(self, replies):
        client = MagicMock()
        client.chat.completions.create.side_effect = [
            MagicMock(choices=[MagicMock(message=MagicMock(content=content),
                                         finish_reason=finish)])
            for content, finish in replies]
        module = MagicMock()
        module.client = client
        return client, patch.dict(sys.modules, {"cqc_lem.utilities.ai.client": module})

    def test_a_reasoning_judge_that_truncates_is_retried_before_it_is_written_off(self,
                                                                                  monkeypatch):
        # `lem-medium` is served by gpt-oss:120b, whose reasoning is billed against max_tokens — at
        # the old 200-token budget EVERY verdict came back empty and the whole run read as unscored.
        monkeypatch.setenv("BENCHMARK_TRUNCATION_RETRIES", "2")
        monkeypatch.setenv("BENCHMARK_JUDGE_MAX_TOKENS", "900")
        client, patched = self._judge_client([("", "length"),
                                              ('{"pass": true, "reasoning": "ok"}', "stop")])
        with patched:
            verdict = bm.fallback_judge(_suite(), _case(), "out")
        assert verdict["passes"] is True and verdict["status"] == "scored"
        assert [c.kwargs["max_tokens"]
                for c in client.chat.completions.create.call_args_list] == [900, 1800]
        # A retry is a real billed call — the run's cap has to see both of them.
        assert verdict["judge_calls"] == 2

    def test_a_judge_that_never_stops_reasoning_is_unscored_but_still_reachable(self, monkeypatch):
        # JUDGE_UNAVAILABLE would stop the runner asking for the REST of the run; one over-thought
        # answer is not a dead judge.
        monkeypatch.setenv("BENCHMARK_TRUNCATION_RETRIES", "1")
        client, patched = self._judge_client([("", "length"), ("", "length")])
        with patched:
            verdict = bm.fallback_judge(_suite(), _case(), "out")
        assert verdict["passes"] is None and verdict["status"] == bm.JUDGE_TIMEOUT
        assert client.chat.completions.create.call_count == 2
        assert verdict["judge_calls"] == 2

    def test_fallback_judge_returns_unscored_when_the_client_fails(self):
        with patch.dict(sys.modules, {}):
            with patch("builtins.__import__", side_effect=ImportError("no client")):
                verdict = bm.fallback_judge(_suite(), _case(), "out")
        # Unscored either way, but distinctly "unreachable" so the runner stops re-asking.
        assert verdict["passes"] is None and verdict["status"] == bm.JUDGE_UNAVAILABLE


# ───────────────────── PostHog evaluation specs + retrieval ──────────────────────

class TestPostHogEvals:
    def test_spec_is_scoped_to_benchmark_events_only(self):
        spec = bm.evaluation_spec(_suite("lem-medium"))
        keys = {p["key"] for c in spec["conditions"] for p in c["properties"]}
        assert "benchmark_run_id" in keys and "lem_tier" in keys
        assert spec["evaluation_type"] == "llm_judge"
        assert spec["conditions"][0]["rollout_percentage"] == 100

    def test_plan_creates_only_what_is_missing(self):
        specs = [bm.evaluation_spec(_suite("lem-simple")), bm.evaluation_spec(_suite("lem-medium"))]
        existing = {bm.evaluation_name("lem-simple"): {"id": "e1"}}
        plan = bm.plan_evaluations(existing, specs)
        assert [p["action"] for p in plan] == ["none", "create"]
        assert plan[0]["id"] == "e1"

    def test_hogql_is_scoped_to_the_run_and_strips_injection(self):
        query = bm.hogql_for_run("bm-1'; DROP TABLE events; --")
        assert "DROP" not in query.upper().replace("DROPTABLEEVENTS", "")
        assert "'bm-1DROPTABLEevents--'" in query
        assert "$ai_evaluation" in query

    def test_merge_judge_events_ignores_other_models_and_tiers(self):
        rows = [["c1", "lem-simple", "new", True, "good"],
                ["c2", "lem-simple", "old", False, "bad"],
                ["c3", "lem-medium", "new", True, "other tier"],
                ["bad-row"]]
        merged = bm.merge_judge_events(rows, "new", "lem-simple")
        assert list(merged) == ["c1"]
        assert merged["c1"]["passes"] is True

    def test_merge_judge_events_accepts_string_results(self):
        merged = bm.merge_judge_events([["c1", "lem-simple", "new", "PASS", ""]],
                                       "new", "lem-simple")
        assert merged["c1"]["passes"] is True
        assert bm.merge_judge_events([["c1", "lem-simple", "new", "maybe", ""]],
                                     "new", "lem-simple") == {}

    def test_event_properties_always_carry_the_run_id(self):
        props = bm.benchmark_event_properties("bm-1", "lem-simple", "c1", "m", "candidate",
                                              1500, {"prompt_tokens": 10})
        assert props["benchmark_run_id"] == "bm-1"
        assert props["source"] == bm.BENCHMARK_SOURCE
        assert props["$ai_latency"] == 1.5
        assert props["$ai_input_tokens"] == 10

    def test_polling_stops_at_the_deadline_and_returns_what_landed(self):
        evals = MagicMock()
        evals.query.return_value = [["c1", "lem-simple", "m", True, ""]]
        rows = bm.poll_judge_results(evals, "bm-1", expected=5, timeout_seconds=1,
                                     sleep=lambda _: time.sleep(0.05))
        assert rows == [["c1", "lem-simple", "m", True, ""]]

    def test_polling_returns_early_once_every_verdict_landed(self):
        evals = MagicMock()
        evals.query.return_value = [["c1", "lem-simple", "m", True, ""]]
        bm.poll_judge_results(evals, "bm-1", expected=1, timeout_seconds=60,
                              sleep=lambda _: None)
        assert evals.query.call_count == 1

    def test_a_failing_query_returns_rather_than_raising(self):
        evals = MagicMock()
        evals.query.side_effect = RuntimeError("posthog down")
        assert bm.poll_judge_results(evals, "bm-1", 3, 5, sleep=lambda _: None) == []


# ──────────────────────────────── thin I/O layer ─────────────────────────────────

class TestProviderClient:
    def _client(self, response=None, error=None):
        provider = bm.ProviderClient("https://ollama.example", "key")
        openai_client = MagicMock()
        if error:
            openai_client.chat.completions.create.side_effect = error
        else:
            openai_client.chat.completions.create.return_value = response
        provider._client = openai_client
        return provider, openai_client

    def test_a_completion_returns_text_latency_and_usage(self):
        response = MagicMock()
        response.choices = [MagicMock(message=MagicMock(content="  hello  "))]
        response.usage = MagicMock(prompt_tokens=3, completion_tokens=4, total_tokens=7)
        provider, openai_client = self._client(response)
        result = provider.complete("m", [{"role": "user", "content": "hi"}], {"temperature": 0})
        assert result["text"] == "hello" and result["error"] is None
        assert result["usage"]["total_tokens"] == 7
        assert result["latency_ms"] >= 0
        assert openai_client.chat.completions.create.call_args.kwargs["temperature"] == 0

    def test_a_provider_failure_is_a_result_not_an_exception(self):
        provider, _ = self._client(error=RuntimeError("410 model retired"))
        result = provider.complete("m", [{"role": "user", "content": "hi"}])
        assert result["text"] is None
        assert "410" in result["error"]

    @staticmethod
    def _reply(content, finish_reason="stop"):
        response = MagicMock()
        response.choices = [MagicMock(message=MagicMock(content=content),
                                      finish_reason=finish_reason)]
        response.usage = MagicMock(prompt_tokens=1, completion_tokens=1, total_tokens=2)
        return response

    def test_a_reasoning_model_that_spends_its_budget_thinking_is_retried_at_double(self,
                                                                                   monkeypatch):
        # minimax-m3 / glm-5.2 bill chain-of-thought against max_tokens: at the fixture's budget the
        # reply comes back EMPTY, which would score as "produced nothing" and reject the candidate
        # for a harness artifact rather than for its quality.
        monkeypatch.setenv("BENCHMARK_TRUNCATION_RETRIES", "2")
        provider, openai_client = self._client()
        openai_client.chat.completions.create.side_effect = [
            self._reply("", finish_reason="length"),
            self._reply("", finish_reason="length"),
            self._reply("the long-form draft"),
        ]
        result = provider.complete("minimax-m3", [{"role": "user", "content": "hi"}],
                                   {"max_tokens": 1400})
        assert result["text"] == "the long-form draft"
        assert result["budget_escalations"] == 2
        budgets = [c.kwargs["max_tokens"]
                   for c in openai_client.chat.completions.create.call_args_list]
        assert budgets == [1400, 2800, 5600]

    def test_the_retry_is_bounded_and_an_answer_that_never_arrives_is_no_output(self, monkeypatch):
        # Grading "" would render as `min_chars: 0 chars < 700` — a quality verdict on a model that
        # was never measured. A completion that stayed empty through the escalations AND the
        # re-measurement is NO OUTPUT, which the gate reads as "the provider didn't answer" (#910).
        monkeypatch.setenv("BENCHMARK_TRUNCATION_RETRIES", "1")
        monkeypatch.setenv("BENCHMARK_EMPTY_REPEATS", "1")
        provider, openai_client = self._client()
        openai_client.chat.completions.create.return_value = self._reply("",
                                                                         finish_reason="length")
        result = provider.complete("m", [{"role": "user", "content": "hi"}], {"max_tokens": 100})
        assert result["text"] is None
        assert "empty completion" in result["error"]
        assert result["budget_escalations"] == 1 and result["repeats"] == 1
        assert openai_client.chat.completions.create.call_count == 3

    def test_a_truncated_but_non_empty_answer_is_never_re_rolled(self, monkeypatch):
        # Retrying a model that DID answer would hand the verbose ones a second attempt the concise
        # ones never got — the comparison has to stay one call per case wherever an answer exists.
        monkeypatch.setenv("BENCHMARK_TRUNCATION_RETRIES", "2")
        provider, openai_client = self._client(self._reply("half a dr", finish_reason="length"))
        result = provider.complete("m", [{"role": "user", "content": "hi"}], {"max_tokens": 100})
        assert result["text"] == "half a dr" and result["budget_escalations"] == 0
        assert openai_client.chat.completions.create.call_count == 1

    def test_a_case_with_no_max_tokens_is_never_escalated(self, monkeypatch):
        monkeypatch.setenv("BENCHMARK_TRUNCATION_RETRIES", "2")
        monkeypatch.setenv("BENCHMARK_EMPTY_REPEATS", "0")
        provider, openai_client = self._client(self._reply("", finish_reason="length"))
        result = provider.complete("m", [{"role": "user", "content": "hi"}], {"temperature": 0.8})
        assert result["budget_escalations"] == 0
        assert openai_client.chat.completions.create.call_count == 1

    def test_a_production_budget_case_is_never_retried_at_a_larger_one(self, monkeypatch):
        # `lem-simple`'s relevance check is `max_tokens: 3` because `ai_helper.py`'s is. A model
        # that cannot answer inside that budget is DISQUALIFIED at that call site — scoring it at 6
        # or 12 tokens credits it with something LEM could never run there (#910).
        monkeypatch.setenv("BENCHMARK_TRUNCATION_RETRIES", "2")
        monkeypatch.setenv("BENCHMARK_EMPTY_REPEATS", "0")
        provider, openai_client = self._client(self._reply("", finish_reason="length"))
        result = provider.complete("m", [{"role": "user", "content": "hi"}], {"max_tokens": 3},
                                   allow_budget_escalation=False)
        assert result["budget_escalations"] == 0 and result["budget_locked"] is True
        assert openai_client.chat.completions.create.call_count == 1
        assert openai_client.chat.completions.create.call_args.kwargs["max_tokens"] == 3

    def test_an_empty_completion_is_re_measured_at_the_same_budget(self, monkeypatch):
        # minimax-m3 returned empty twice in bm-20260802-20ae40 and answered both cases on a re-run
        # at the same effective budget. The repeat re-ASKS, it does not buy headroom.
        monkeypatch.setenv("BENCHMARK_TRUNCATION_RETRIES", "0")
        monkeypatch.setenv("BENCHMARK_EMPTY_REPEATS", "1")
        provider, openai_client = self._client()
        openai_client.chat.completions.create.side_effect = [
            self._reply("", finish_reason="length"), self._reply("the long-form draft")]
        result = provider.complete("minimax-m3", [{"role": "user", "content": "hi"}],
                                   {"max_tokens": 1400})
        assert result["text"] == "the long-form draft"
        assert result["repeats"] == 1 and result["budget_escalations"] == 0
        budgets = [c.kwargs["max_tokens"]
                   for c in openai_client.chat.completions.create.call_args_list]
        assert budgets == [1400, 1400]

    def test_a_production_budget_case_is_still_re_measured_when_it_comes_back_empty(self,
                                                                                    monkeypatch):
        monkeypatch.setenv("BENCHMARK_EMPTY_REPEATS", "1")
        provider, openai_client = self._client()
        openai_client.chat.completions.create.side_effect = [
            self._reply("", finish_reason="length"), self._reply("yes")]
        result = provider.complete("m", [{"role": "user", "content": "hi"}], {"max_tokens": 3},
                                   allow_budget_escalation=False)
        assert result["text"] == "yes" and result["repeats"] == 1
        # Measured fine on the repeat, so it is NOT reported as a case the budget locked out.
        assert result["budget_locked"] is False
        assert [c.kwargs["max_tokens"]
                for c in openai_client.chat.completions.create.call_args_list] == [3, 3]

    def test_a_discarded_attempt_is_not_charged_to_the_model_s_latency(self, monkeypatch):
        # p50/p90 go into the rolling leaderboard forever. Charging the thrown-away attempt to the
        # model would inflate exactly the reasoning models this retry exists to measure fairly.
        monkeypatch.setenv("BENCHMARK_TRUNCATION_RETRIES", "1")
        provider, openai_client = self._client()
        openai_client.chat.completions.create.side_effect = [
            self._reply("", finish_reason="length"), self._reply("the draft")]
        # attempt-1 start, attempt-2 start, end-of-attempt-2
        monkeypatch.setattr(bm.time, "time", MagicMock(side_effect=[100.0, 140.0, 140.25]))
        result = provider.complete("m", [{"role": "user", "content": "hi"}], {"max_tokens": 100})
        assert result["budget_escalations"] == 1
        assert result["latency_ms"] == pytest.approx(250.0)


class TestPostHogClient:
    def _client(self, payload):
        client = bm.PostHogEvals("personal-key", "475262")
        requests = MagicMock()
        response = MagicMock(content=b"{}")
        response.json.return_value = payload
        requests.request.return_value = response
        return client, requests

    def test_list_evaluations_is_keyed_by_name(self):
        client, requests = self._client({"results": [{"name": "LEM benchmark — lem-simple",
                                                      "id": "e1", "enabled": True},
                                                     {"id": "e2"}]})
        with patch.dict(sys.modules, {"requests": requests}):
            assert client.list_evaluations() == {"LEM benchmark — lem-simple":
                                                 {"id": "e1", "enabled": True}}

    def test_query_posts_hogql_to_the_project_query_endpoint(self):
        client, requests = self._client({"results": [["c1"]]})
        with patch.dict(sys.modules, {"requests": requests}):
            assert client.query("SELECT 1") == [["c1"]]
        kwargs = requests.request.call_args.kwargs
        assert kwargs["json"]["query"]["kind"] == "HogQLQuery"
        assert "/api/projects/475262/query/" in requests.request.call_args.args[1]

    def test_run_evaluation_passes_the_event_id(self):
        client, requests = self._client({"workflow_id": "w1"})
        with patch.dict(sys.modules, {"requests": requests}):
            assert client.run_evaluation("e1", "evt-1") == "w1"
        assert requests.request.call_args.kwargs["json"] == {"event_id": "evt-1"}


class TestEmission:
    CASE = {"id": "c1", "messages": [{"role": "user", "content": "hi"}]}
    COMPLETION = {"text": "answer", "latency_ms": 1200.0, "usage": {"prompt_tokens": 5}}

    def test_no_project_key_means_no_event_and_no_id(self, monkeypatch):
        monkeypatch.delenv("POSTHOG_API_KEY", raising=False)
        posthog = MagicMock()
        with patch.dict(sys.modules, {"posthog": posthog}):
            assert bm.emit_generation("bm-1", "lem-simple", self.CASE, "m", "candidate",
                                      self.COMPLETION) is None
        assert posthog.capture.called is False

    def test_a_tagged_generation_carries_the_run_id_and_the_anonymous_distinct_id(self,
                                                                                  monkeypatch):
        monkeypatch.setenv("POSTHOG_API_KEY", "phc_test")
        posthog = MagicMock()
        with patch.dict(sys.modules, {"posthog": posthog}):
            event_id = bm.emit_generation("bm-1", "lem-simple", self.CASE, "m", "candidate",
                                          self.COMPLETION)
        kwargs = posthog.capture.call_args.kwargs
        assert kwargs["event"] == "$ai_generation"
        assert kwargs["distinct_id"] == bm.BENCHMARK_DISTINCT_ID
        assert kwargs["properties"]["benchmark_run_id"] == "bm-1"
        assert kwargs["uuid"] == event_id

    def test_an_sdk_that_rejects_a_uuid_leaves_the_generation_unjudgeable(self, monkeypatch):
        monkeypatch.setenv("POSTHOG_API_KEY", "phc_test")
        posthog = MagicMock()
        posthog.capture.side_effect = [TypeError("no uuid kwarg"), None]
        with patch.dict(sys.modules, {"posthog": posthog}):
            assert bm.emit_generation("bm-1", "lem-simple", self.CASE, "m", "candidate",
                                      self.COMPLETION) is None
        assert posthog.capture.call_count == 2

    def test_an_emission_failure_is_swallowed(self, monkeypatch, capsys):
        monkeypatch.setenv("POSTHOG_API_KEY", "phc_test")
        posthog = MagicMock()
        posthog.capture.side_effect = RuntimeError("network down")
        with patch.dict(sys.modules, {"posthog": posthog}):
            assert bm.emit_generation("bm-1", "lem-simple", self.CASE, "m", "candidate",
                                      self.COMPLETION) is None
        assert "PostHog emission failed" in capsys.readouterr().err

    def test_fallback_metrics_are_emitted_as_ai_metric(self, monkeypatch):
        monkeypatch.setenv("POSTHOG_API_KEY", "phc_test")
        posthog = MagicMock()
        with patch.dict(sys.modules, {"posthog": posthog}):
            bm.emit_metric("bm-1", "lem-simple", "c1", "m", {"passes": True})
        properties = posthog.capture.call_args.kwargs["properties"]
        assert posthog.capture.call_args.kwargs["event"] == "$ai_metric"
        assert properties["$ai_metric_value"] is True
        assert properties["benchmark_run_id"] == "bm-1"

    def test_metric_emission_is_a_no_op_without_a_key(self, monkeypatch):
        monkeypatch.delenv("POSTHOG_API_KEY", raising=False)
        posthog = MagicMock()
        with patch.dict(sys.modules, {"posthog": posthog}):
            bm.emit_metric("bm-1", "lem-simple", "c1", "m", {"passes": True})
        assert posthog.capture.called is False


class TestEnvGates:
    def test_benchmark_enabled_is_off_by_default(self, monkeypatch):
        monkeypatch.delenv("BENCHMARK_ENABLED", raising=False)
        assert bm.benchmark_enabled() is False
        monkeypatch.setenv("BENCHMARK_ENABLED", "true")
        assert bm.benchmark_enabled() is True

    def test_numeric_env_gates_clamp_and_fall_back(self, monkeypatch):
        monkeypatch.setenv("BENCHMARK_MAX_JUDGE_CALLS", "not-a-number")
        assert bm.max_judge_calls() == 200
        monkeypatch.setenv("BENCHMARK_MAX_JUDGE_CALLS", "999999")
        assert bm.max_judge_calls() == 5000
        monkeypatch.setenv("BENCHMARK_JUDGE_TIMEOUT_SECONDS", "1")
        assert bm.judge_timeout_seconds() == 5  # floored, never an instant give-up


# ─────────────────────────── champions from the config ───────────────────────────

CONFIG = """
model_list:
  - model_name: lem-simple
    litellm_params:
      model: openai/gpt-oss:20b
      api_base: os.environ/OLLAMA_CLOUD_URL
  - model_name: lem-simple
    litellm_params:
      model: openai/gpt-4o-mini
      api_key: os.environ/OPENAI_API_KEY
  - model_name: lem-medium
    litellm_params:
      model: openai/gpt-oss:120b
      api_base: os.environ/OLLAMA_CLOUD_URL
  - model_name: lem-image
    litellm_params:
      model: openai/dall-e-3
      api_key: os.environ/OPENAI_API_KEY
"""


class TestChampions:
    def test_first_ollama_deployment_per_tier_wins(self):
        champions = bm.champions_from_config(CONFIG, ["lem-simple", "lem-medium", "lem-complex"])
        assert champions == {"lem-simple": "gpt-oss:20b", "lem-medium": "gpt-oss:120b"}

    def test_the_live_config_still_names_a_champion_for_every_tier(self):
        text = (_ROOT / ".litellm" / "config.yaml").read_text()
        champions = bm.champions_from_config(text, list(bm.TIERS))
        assert set(champions) == set(bm.TIERS)

    def test_champion_overrides_parse_and_validate(self):
        assert bm.parse_champion_overrides("lem-simple=a,lem-medium=b") == {
            "lem-simple": "a", "lem-medium": "b"}
        assert bm.parse_champion_overrides("") == {}
        with pytest.raises(SystemExit):
            bm.parse_champion_overrides("lem-simple")


# ─────────────────────── usage level / quota delta (#842) ────────────────────────

class TestUsageLevels:
    def test_the_label_scale_matches_the_health_checks(self):
        # Both halves must mean the same thing by "medium" — an override and a scraped page feed
        # the same comparison.
        import importlib.util as _iu
        path = _ROOT / "scripts" / "model_health_check.py"
        spec = _iu.spec_from_file_location("model_health_check_for_usage_scale", path)
        mhc = _iu.module_from_spec(spec)
        spec.loader.exec_module(mhc)
        assert bm.USAGE_LEVELS_BY_LABEL == mhc._USAGE_LEVELS

    def test_usage_name_renders_the_level_and_never_guesses_a_middle(self):
        assert bm.usage_name({"level": 3, "label": "high"}) == "High (3)"
        assert bm.usage_name({"level": 2, "label": None}) == "Medium (2)"
        assert bm.usage_name({"level": None, "label": None}) == "unknown"
        assert bm.usage_name(None) == "unknown"

    def test_overrides_parse_and_validate(self):
        assert bm.parse_usage_overrides("gpt-oss:120b=medium,qwen3.5:397b=Medium") == {
            "gpt-oss:120b": {"level": 2, "label": "medium"},
            "qwen3.5:397b": {"level": 2, "label": "medium"}}
        assert bm.parse_usage_overrides("") == {}
        with pytest.raises(SystemExit):
            bm.parse_usage_overrides("gpt-oss:120b=enormous")
        with pytest.raises(SystemExit):
            bm.parse_usage_overrides("gpt-oss:120b")

    def test_collect_prefers_the_override_over_the_page(self):
        fetch = MagicMock(return_value={"level": 3, "label": "high"})
        levels = bm.collect_usage_levels(["a", "b"], fetch=fetch,
                                         overrides={"a": {"level": 2, "label": "medium"}})
        assert levels["a"] == {"level": 2, "label": "medium"}
        assert levels["b"] == {"level": 3, "label": "high"}
        fetch.assert_called_once_with("b")

    def test_no_fetch_means_no_network_and_unknown_levels(self):
        assert bm.collect_usage_levels(["a"]) == {"a": {"level": None, "label": None}}

    def test_a_failing_fetch_leaves_the_model_unknown_rather_than_failing_the_run(self, capsys):
        levels = bm.collect_usage_levels(["a"], fetch=MagicMock(side_effect=RuntimeError("503")))
        assert levels == {"a": {"level": None, "label": None}}
        assert "usage level unavailable" in capsys.readouterr().err

    def test_a_higher_usage_candidate_is_reported_as_a_quota_increase(self):
        delta = bm.usage_delta({"level": 3, "label": "high"}, {"level": 2, "label": "medium"},
                               candidate_model="minimax-m3", champion_model="qwen3.5:397b")
        assert delta["direction"] == bm.USAGE_UP and delta["steps"] == 1
        assert "quota increase" in delta["summary"] and "qwen3.5:397b" in delta["summary"]

    def test_equal_levels_are_flat_and_a_lower_level_is_a_decrease(self):
        flat = bm.usage_delta({"level": 2}, {"level": 2})
        assert flat["direction"] == bm.USAGE_FLAT and flat["steps"] == 0
        down = bm.usage_delta({"level": 1, "label": "low"}, {"level": 2, "label": "medium"})
        assert down["direction"] == bm.USAGE_DOWN and down["steps"] == -1

    def test_an_unreadable_level_is_unknown_not_flat(self):
        # The whole point: "we could not read it" must never render as "it costs the same".
        delta = bm.usage_delta({"level": None}, {"level": 2}, candidate_model="glm-5.2",
                               champion_model="gpt-oss:120b")
        assert delta["direction"] == bm.USAGE_UNKNOWN and delta["steps"] is None
        assert "unknown" in delta["summary"] and "glm-5.2" in delta["summary"]
        assert "--usage-levels glm-5.2=" in delta["summary"]

    def test_an_off_scale_level_is_unreadable_in_the_delta_too_not_just_in_the_table(self):
        # `parse_usage_level` counts filled pips when the page's wording changes, so a markup drift
        # yields a level like 7. The scorecard already prints that as `unknown`; if the delta still
        # believed the number, an unread CHAMPION level would render a real increase as a
        # "decrease" — and a decrease is exactly what the standing spend policy waves through.
        champion = {"level": 7, "label": None}
        candidate = {"level": 3, "label": "high"}
        assert bm.usage_name(champion) == "unknown"
        delta = bm.usage_delta(candidate, champion, candidate_model="minimax-m3",
                               champion_model="qwen3.5:397b")
        assert delta["direction"] == bm.USAGE_UNKNOWN and delta["steps"] is None
        assert delta["champion_level"] is None  # the JSON says what the table says
        assert "qwen3.5:397b" in delta["summary"]
        assert bm.quota_policy("lem-medium", delta, {"judge_pass_rate": 1.0},
                               {"judge_pass_rate": 0.1})["decision"] == bm.POLICY_HOLD

    def test_a_level_replayed_from_json_still_compares(self):
        # `--render` replays a results file; a level that came back as 3.0 must not read as unknown
        # in the delta while the table happily prints "High (3)".
        delta = bm.usage_delta({"level": 3.0}, {"level": 2.0})
        assert delta["direction"] == bm.USAGE_UP and delta["steps"] == 1

    def test_the_extra_high_label_renders_the_way_the_scale_names_it(self):
        assert bm.usage_name({"level": 4, "label": "extra high"}) == "Extra high (4)"

    def test_the_gate_reports_the_delta_without_blocking_on_it(self):
        candidate = dict(_card(model="minimax-m3"), usage={"level": 3, "label": "high"})
        champion = dict(_card(model="qwen3.5:397b", role="champion"),
                        usage={"level": 2, "label": "medium"})
        gate = bm.gate_decision(candidate, champion, {"min_judged": 1})
        assert gate["verdict"] == bm.VERDICT_RECOMMEND  # quality still wins the gate…
        assert gate["usage_delta"]["direction"] == bm.USAGE_UP  # …but the price rides along
        assert not any(e["expectation"].startswith("usage") for e in gate["expectations"])

    def test_recommendations_carry_the_quota_delta_for_the_cron(self):
        candidate = dict(_card(model="minimax-m3"), usage={"level": 3, "label": "high"})
        champion = dict(_card(model="qwen3.5:397b", role="champion"),
                        usage={"level": 2, "label": "medium"})
        recs = bm.swap_recommendations([bm.gate_decision(candidate, champion, {"min_judged": 1})])
        assert recs[0]["usage_delta"]["direction"] == bm.USAGE_UP

    def test_scorecards_carry_the_level_through_a_run(self):
        run = bm.run_benchmark(
            {"lem-simple": _suite("lem-simple", cases=[
                _case("a", assertions=[{"type": "max_chars", "value": 50}],
                      canned={"output": "ok", "judge_pass": True})], thresholds={"min_judged": 1})},
            ["cand"], {"lem-simple": "champ"}, run_id="bm-u", today="2026-07-27",
            dry_run=True, judge_cap=5,
            usage_levels={"cand": {"level": 3, "label": "high"},
                          "champ": {"level": 2, "label": "medium"}})
        assert {c["model"]: c["usage"]["level"] for c in run["scorecards"]} == {
            "cand": 3, "champ": 2}
        assert run["usage_levels"]["cand"]["label"] == "high"

    def test_the_report_shows_the_level_beside_the_scores_and_warns_on_an_increase(self):
        candidate = dict(_card(model="minimax-m3"), usage={"level": 3, "label": "high"})
        champion = dict(_card(model="qwen3.5:397b", role="champion"),
                        usage={"level": 2, "label": "medium"})
        gate = bm.gate_decision(candidate, champion, {"min_judged": 1})
        report = bm.render_report({
            "source": bm.BENCHMARK_SOURCE, "run_id": "bm-x", "date": "2026-07-27",
            "scoring_mode": bm.SCORING_FALLBACK, "tiers": ["lem-simple"],
            "candidates": ["minimax-m3"], "scorecards": [champion, candidate],
            "gates": [gate], "recommendations": bm.swap_recommendations([gate])})
        assert "| Usage |" in report
        assert "High (3)" in report and "Medium (2)" in report
        assert "quota increase" in report
        assert "raises this stack's Ollama Cloud quota class" in report

    def test_a_flat_recommendation_gets_no_quota_warning_block(self):
        candidate = dict(_card(model="deepseek-v4-flash"), usage={"level": 2, "label": "medium"})
        champion = dict(_card(model="gpt-oss:120b", role="champion"),
                        usage={"level": 2, "label": "medium"})
        gate = bm.gate_decision(candidate, champion, {"min_judged": 1})
        report = bm.render_report({
            "source": bm.BENCHMARK_SOURCE, "run_id": "bm-x", "date": "2026-07-27",
            "scoring_mode": bm.SCORING_FALLBACK, "tiers": ["lem-simple"],
            "candidates": ["deepseek-v4-flash"], "scorecards": [champion, candidate],
            "gates": [gate], "recommendations": bm.swap_recommendations([gate])})
        assert "flat on quota" in report
        assert "raises this stack's Ollama Cloud quota class" not in report


# ────────────────── standing spend policy for a quota increase (#842 `2A`) ───────────────────

class TestQuotaPolicy:
    """The owner's standing call: a usage-level increase is adoptable only on `lem-complex`, and
    only on a STRICT judge-rate win. Encoded so an unattended run states the decision itself."""

    def test_no_increase_is_adoptable_on_the_quality_gate_alone(self):
        for direction in (bm.USAGE_FLAT, bm.USAGE_DOWN):
            policy = bm.quota_policy("lem-medium", {"direction": direction})
            assert policy["decision"] == bm.POLICY_ADOPT

    def test_a_strict_judge_win_on_lem_complex_is_adoptable(self):
        policy = bm.quota_policy("lem-complex", {"direction": bm.USAGE_UP},
                                 _card(tier="lem-complex", judge_rate=0.9),
                                 _card(tier="lem-complex", role="champion", judge_rate=0.8))
        assert policy["decision"] == bm.POLICY_ADOPT
        assert "90% vs 80%" in policy["reason"]

    def test_a_tie_on_lem_complex_is_held_because_a_tie_does_not_buy_a_usage_level(self):
        policy = bm.quota_policy("lem-complex", {"direction": bm.USAGE_UP},
                                 _card(tier="lem-complex", judge_rate=0.9),
                                 _card(tier="lem-complex", role="champion", judge_rate=0.9))
        assert policy["decision"] == bm.POLICY_HOLD
        assert "tie" in policy["reason"]

    def test_an_increase_on_any_other_tier_is_held_however_good_the_candidate(self):
        policy = bm.quota_policy("lem-medium", {"direction": bm.USAGE_UP},
                                 _card(tier="lem-medium", judge_rate=1.0),
                                 _card(tier="lem-medium", role="champion", judge_rate=0.5))
        assert policy["decision"] == bm.POLICY_HOLD
        assert "`lem-complex`" in policy["reason"] and "lem-medium" in policy["reason"]

    def test_a_missing_judge_rate_cannot_clear_a_quota_increase(self):
        policy = bm.quota_policy("lem-complex", {"direction": bm.USAGE_UP},
                                 _card(tier="lem-complex", judge_rate=None),
                                 _card(tier="lem-complex", role="champion", judge_rate=0.8))
        assert policy["decision"] == bm.POLICY_HOLD
        assert "measured quality win" in policy["reason"]

    def test_an_unread_level_is_held_rather_than_adopted(self):
        # Symmetric with `usage_delta`: unknown is treated as an increase, and a policy about
        # increases cannot clear one whose size nobody can see.
        for delta in ({"direction": bm.USAGE_UNKNOWN}, {}, None):
            policy = bm.quota_policy("lem-complex", delta,
                                     _card(tier="lem-complex", judge_rate=1.0),
                                     _card(tier="lem-complex", role="champion", judge_rate=0.1))
            assert policy["decision"] == bm.POLICY_HOLD
            assert "BENCHMARK_USAGE_LEVELS" in policy["reason"]

    def test_the_gate_and_the_recommendation_both_carry_the_policy(self):
        candidate = dict(_card(tier="lem-complex", model="minimax-m3", judge_rate=0.9),
                         usage={"level": 3, "label": "high"})
        champion = dict(_card(tier="lem-complex", model="qwen3.5:397b", role="champion",
                              judge_rate=0.8), usage={"level": 2, "label": "medium"})
        gate = bm.gate_decision(candidate, champion, {"min_judged": 1})
        assert gate["verdict"] == bm.VERDICT_RECOMMEND
        assert gate["quota_policy"]["decision"] == bm.POLICY_ADOPT
        assert bm.swap_recommendations([gate])[0]["quota_policy"]["decision"] == bm.POLICY_ADOPT

    def test_a_gate_with_no_policy_recorded_still_ships_one(self):
        # A consumer of the JSON must never read a missing key as "adopt".
        recs = bm.swap_recommendations([{"tier": "lem-medium", "model": "a", "champion": "old",
                                         "verdict": bm.VERDICT_RECOMMEND}])
        assert recs[0]["quota_policy"]["decision"] == bm.POLICY_HOLD

    def test_the_report_names_the_held_recommendations(self):
        candidate = dict(_card(tier="lem-medium", model="minimax-m3"),
                         usage={"level": 3, "label": "high"})
        champion = dict(_card(tier="lem-medium", model="gpt-oss:120b", role="champion"),
                        usage={"level": 2, "label": "medium"})
        gate = bm.gate_decision(candidate, champion, {"min_judged": 1})
        report = bm.render_report({
            "source": bm.BENCHMARK_SOURCE, "run_id": "bm-x", "date": "2026-07-27",
            "scoring_mode": bm.SCORING_FALLBACK, "tiers": ["lem-medium"],
            "candidates": ["minimax-m3"], "scorecards": [champion, candidate],
            "gates": [gate], "recommendations": bm.swap_recommendations([gate])})
        assert "standing spend policy" in report
        assert "**1 held**" in report and "`minimax-m3` on lem-medium" in report

    def test_an_adoptable_recommendation_is_not_reported_as_held(self):
        candidate = dict(_card(tier="lem-complex", model="minimax-m3", judge_rate=0.9),
                         usage={"level": 3, "label": "high"})
        champion = dict(_card(tier="lem-complex", model="qwen3.5:397b", role="champion",
                              judge_rate=0.8), usage={"level": 2, "label": "medium"})
        gate = bm.gate_decision(candidate, champion, {"min_judged": 1})
        report = bm.render_report({
            "source": bm.BENCHMARK_SOURCE, "run_id": "bm-x", "date": "2026-07-27",
            "scoring_mode": bm.SCORING_FALLBACK, "tiers": ["lem-complex"],
            "candidates": ["minimax-m3"], "scorecards": [champion, candidate],
            "gates": [gate], "recommendations": bm.swap_recommendations([gate])})
        assert "**adopt**" in report and "held**" not in report


# ──────────────────────────────── orchestration ──────────────────────────────────

class TestRunBenchmark:
    def _suites(self):
        return {"lem-simple": _suite("lem-simple", cases=[
            _case("a", assertions=[{"type": "max_chars", "value": 50}],
                  canned={"output": "short answer", "judge_pass": True, "latency_ms": 100}),
            _case("b", assertions=[{"type": "max_chars", "value": 5}],
                  canned={"output": "far too long to fit", "judge_pass": False}),
        ], thresholds={"min_judged": 1})}

    def test_dry_run_scores_canned_outputs_with_no_network(self):
        run = bm.run_benchmark(self._suites(), ["cand"], {"lem-simple": "champ"},
                               run_id="bm-1", today="2026-07-27", provider=None, evals=None,
                               judge_cap=10, dry_run=True)
        assert run["scoring_mode"] == bm.SCORING_DRY_RUN
        assert {c["model"] for c in run["scorecards"]} == {"champ", "cand"}
        for card in run["scorecards"]:
            assert card["deterministic_pass_rate"] == 0.5
            assert card["judged"] == 1  # only the passing case is judged
            assert card["benchmark_run_id"] == "bm-1"
        assert bm.render_report(run)

    def test_a_run_with_no_candidates_is_a_champion_baseline(self):
        run = bm.run_benchmark(self._suites(), [], {"lem-simple": "champ"},
                               run_id="bm-1", today="2026-07-27", dry_run=True, judge_cap=10)
        assert [c["role"] for c in run["scorecards"]] == ["champion"]
        assert run["gates"] == [] and run["recommendations"] == []

    def test_provider_errors_land_on_the_case_and_block_the_gate(self):
        provider = MagicMock()
        provider.complete.return_value = {"text": None, "error": "410 model retired",
                                          "latency_ms": 5.0, "usage": {}}
        run = bm.run_benchmark(self._suites(), ["cand"], {"lem-simple": "champ"},
                               run_id="bm-1", today="2026-07-27", provider=provider,
                               judge_cap=10)
        card = next(c for c in run["scorecards"] if c["role"] == "candidate")
        assert card["errors"] == ["a", "b"]
        assert run["gates"][0]["verdict"] == bm.VERDICT_REJECT
        assert run["recommendations"] == []

    def test_a_production_budget_case_reaches_the_provider_with_escalation_off(self):
        suites = {"lem-simple": _suite("lem-simple", cases=[
            _case("mirrors", assertions=[{"type": "max_chars", "value": 50}],
                  params={"max_tokens": 3}, budget_mirrors_production=True),
            _case("free", assertions=[{"type": "max_chars", "value": 50}],
                  params={"max_tokens": 600})], thresholds={"min_judged": 1})}
        provider = MagicMock()
        provider.complete.return_value = {"text": "yes", "error": None, "latency_ms": 5.0,
                                          "usage": {}, "repeats": 0, "budget_locked": False}
        bm.run_benchmark(suites, [], {"lem-simple": "champ"}, run_id="bm-1", today="2026-07-27",
                         provider=provider, judge_cap=0, judge_enabled=False)
        # Suite order: the production-mirror case first, the free-budget one second.
        assert [c.kwargs["allow_budget_escalation"]
                for c in provider.complete.call_args_list] == [False, True]

    def test_re_measurements_and_budget_locks_ride_onto_the_scorecard(self):
        provider = MagicMock()
        provider.complete.return_value = {"text": "short answer", "error": None, "latency_ms": 5.0,
                                          "usage": {}, "repeats": 1, "budget_locked": True}
        run = bm.run_benchmark(self._suites(), [], {"lem-simple": "champ"}, run_id="bm-1",
                               today="2026-07-27", provider=provider, judge_cap=0,
                               judge_enabled=False)
        card = run["scorecards"][0]
        assert card["remeasured_cases"] == ["a", "b"]
        assert card["budget_locked_cases"] == ["a", "b"]

    def test_no_judge_mode_spends_nothing_and_reports_deterministic_only(self):
        provider = MagicMock()
        provider.complete.return_value = {"text": "short", "error": None,
                                          "latency_ms": 5.0, "usage": {}}
        run = bm.run_benchmark(self._suites(), ["cand"], {"lem-simple": "champ"},
                               run_id="bm-1", today="2026-07-27", provider=provider,
                               judge_cap=10, judge_enabled=False)
        assert run["scoring_mode"] == bm.SCORING_DETERMINISTIC
        assert run["judge_calls"] == 0
        assert run["gates"][0]["verdict"] == bm.VERDICT_DETERMINISTIC_ONLY

    def test_the_judge_cap_bounds_the_whole_run(self):
        run = bm.run_benchmark(self._suites(), ["cand"], {"lem-simple": "champ"},
                               run_id="bm-1", today="2026-07-27", dry_run=True, judge_cap=1)
        assert run["judge_calls"] == 1
        assert sum(c["judged"] for c in run["scorecards"]) == 1

    def test_unavailable_posthog_evaluations_degrade_to_the_fallback_judge(self):
        provider = MagicMock()
        provider.complete.return_value = {"text": "short answer", "error": None,
                                          "latency_ms": 5.0, "usage": {}}
        evals = MagicMock()
        evals.list_evaluations.side_effect = RuntimeError("401 no key")
        with patch.object(bm, "fallback_judge",
                          return_value={"passes": True, "status": "scored", "reasoning": ""}) as fj:
            with patch.object(bm, "emit_metric") as metric:
                run = bm.run_benchmark(self._suites(), [], {"lem-simple": "champ"},
                                       run_id="bm-1", today="2026-07-27", provider=provider,
                                       evals=evals, judge_cap=10)
        assert run["scoring_mode"] == bm.SCORING_FALLBACK
        assert fj.called and metric.called

    def test_posthog_verdicts_that_never_land_are_reported_as_timeouts(self):
        provider = MagicMock()
        provider.complete.return_value = {"text": "short answer", "error": None,
                                          "latency_ms": 5.0, "usage": {}}
        evals = MagicMock()
        evals.list_evaluations.return_value = {}
        evals.create_evaluation.return_value = "e1"
        evals.query.return_value = []
        # The polling deadline itself is covered in TestPostHogEvals with an injected sleep; here
        # the point is what an EMPTY result set does to the scorecard when the in-runner judge is
        # unavailable too — unscored, never inferred.
        with patch.object(bm, "emit_generation", return_value="evt-1"), \
                patch.object(bm, "poll_judge_results", return_value=[]), \
                patch.object(bm, "fallback_judge",
                             return_value={"passes": None, "status": bm.JUDGE_TIMEOUT,
                                           "reasoning": "no judge"}):
            run = bm.run_benchmark(self._suites(), [], {"lem-simple": "champ"},
                                   run_id="bm-1", today="2026-07-27", provider=provider,
                                   evals=evals, judge_cap=10)
        card = run["scorecards"][0]
        assert run["scoring_mode"] == bm.SCORING_POSTHOG
        assert card["judge_timeouts"] == 1 and card["judge_pass_rate"] is None
        assert run["gates"] == []

    def test_a_partial_posthog_read_keeps_the_unscored_cases_as_timeouts(self):
        """Rebuilding a card from the returned rows alone used to DROP the cases PostHog never
        scored, rendering 1-of-3 verdicts as a 100% judge pass on full evidence."""
        suites = {"lem-simple": _suite("lem-simple", cases=[
            _case(cid, assertions=[{"type": "max_chars", "value": 50}]) for cid in ("a", "b", "c")],
            thresholds={"min_judged": 1})}
        provider = MagicMock()
        provider.complete.return_value = {"text": "short", "error": None,
                                          "latency_ms": 5.0, "usage": {}}
        evals = MagicMock()
        evals.list_evaluations.return_value = {bm.evaluation_name("lem-simple"): {"id": "e1"}}
        with patch.object(bm, "emit_generation", return_value="evt-1"), \
                patch.object(bm, "poll_judge_results",
                             return_value=[["a", "lem-simple", "champ", True, "ok"]]), \
                patch.object(bm, "fallback_judge",
                             return_value={"passes": None, "status": bm.JUDGE_TIMEOUT,
                                           "reasoning": "no judge"}):
            run = bm.run_benchmark(suites, [], {"lem-simple": "champ"}, run_id="bm-1",
                                   today="2026-07-27", provider=provider, evals=evals,
                                   judge_cap=10)
        card = run["scorecards"][0]
        assert card["judged"] == 1 and card["judge_passed"] == 1
        assert card["judge_timeouts"] == 2

    def test_a_posthog_run_that_scores_nothing_falls_back_to_the_in_runner_judge(self):
        """PostHog's judge needs a provider key of its own (it cannot judge via Ollama Cloud). With
        none configured the evaluations exist but score nothing, so the run degrades rather than
        reporting evidence the gate can only read as absent."""
        provider = MagicMock()
        provider.complete.return_value = {"text": "short answer", "error": None,
                                          "latency_ms": 5.0, "usage": {}}
        evals = MagicMock()
        evals.list_evaluations.return_value = {bm.evaluation_name("lem-simple"): {"id": "e1"}}
        with patch.object(bm, "emit_generation", return_value="evt-1"), \
                patch.object(bm, "poll_judge_results", return_value=[]), \
                patch.object(bm, "emit_metric") as metric, \
                patch.object(bm, "fallback_judge",
                             return_value={"passes": True, "status": "scored",
                                           "reasoning": "grounded"}) as fj:
            run = bm.run_benchmark(self._suites(), [], {"lem-simple": "champ"},
                                   run_id="bm-1", today="2026-07-27", provider=provider,
                                   evals=evals, judge_cap=10)
        card = run["scorecards"][0]
        assert fj.called and metric.called
        assert run["scoring_mode"] == bm.SCORING_FALLBACK
        assert card["judged"] == 1 and card["judge_pass_rate"] == 1.0

    def test_the_fallback_after_a_dead_posthog_judge_stays_inside_the_cap(self):
        provider = MagicMock()
        provider.complete.return_value = {"text": "short", "error": None,
                                          "latency_ms": 5.0, "usage": {}}
        evals = MagicMock()
        evals.list_evaluations.return_value = {bm.evaluation_name("lem-simple"): {"id": "e1"}}
        with patch.object(bm, "emit_generation", return_value="evt-1"), \
                patch.object(bm, "poll_judge_results", return_value=[]), \
                patch.object(bm, "emit_metric"), \
                patch.object(bm, "fallback_judge",
                             return_value={"passes": True, "status": "scored",
                                           "reasoning": ""}) as fj:
            run = bm.run_benchmark(self._suites(), [], {"lem-simple": "champ"},
                                   run_id="bm-1", today="2026-07-27", provider=provider,
                                   evals=evals, judge_cap=1)
        assert fj.call_count == 0  # the single PostHog trigger already spent the cap
        assert run["judge_calls"] == 1

    def test_a_truncation_retried_verdict_spends_the_cap_it_actually_cost(self):
        # `BENCHMARK_MAX_JUDGE_CALLS` is a spend ceiling. A verdict that took three completions to
        # arrive must count three, or a retrying judge overruns the cap by the retry factor with
        # the report still printing "N of 200".
        suites = {"lem-simple": _suite("lem-simple", cases=[
            _case(cid, assertions=[{"type": "max_chars", "value": 50}]) for cid in ("a", "b", "c")],
            thresholds={"min_judged": 1})}
        provider = MagicMock()
        provider.complete.return_value = {"text": "short", "error": None,
                                          "latency_ms": 5.0, "usage": {}}
        with patch.object(bm, "emit_metric"), \
                patch.object(bm, "fallback_judge",
                             return_value={"passes": True, "status": "scored",
                                           "reasoning": "", "judge_calls": 3}) as fj:
            run = bm.run_benchmark(suites, [], {"lem-simple": "champ"}, run_id="bm-1",
                                   today="2026-07-27", provider=provider, evals=None,
                                   judge_cap=4)
        assert fj.call_count == 2  # the second verdict reached the cap; the third case is unscored
        assert run["judge_calls"] == 6

    def test_an_unreachable_in_runner_judge_is_asked_once_not_once_per_case(self):
        suites = {"lem-simple": _suite("lem-simple", cases=[
            _case(cid, assertions=[{"type": "max_chars", "value": 50}]) for cid in ("a", "b", "c")],
            thresholds={"min_judged": 1})}
        provider = MagicMock()
        provider.complete.return_value = {"text": "short", "error": None,
                                          "latency_ms": 5.0, "usage": {}}
        with patch.object(bm, "fallback_judge",
                          return_value={"passes": None, "status": bm.JUDGE_UNAVAILABLE,
                                        "reasoning": "connection refused"}) as fj:
            run = bm.run_benchmark(suites, [], {"lem-simple": "champ"}, run_id="bm-1",
                                   today="2026-07-27", provider=provider, evals=None,
                                   judge_cap=10)
        assert fj.call_count == 1
        assert run["scorecards"][0]["judge_timeouts"] == 3

    def test_a_candidate_that_already_serves_the_tier_is_never_self_compared(self):
        run = bm.run_benchmark(self._suites(), ["champ"], {"lem-simple": "champ"},
                               run_id="bm-1", today="2026-07-27", dry_run=True, judge_cap=10)
        assert [c["role"] for c in run["scorecards"]] == ["champion"]
        assert run["gates"] == [] and run["recommendations"] == []

    def test_posthog_verdicts_are_merged_back_onto_the_right_scorecard(self):
        provider = MagicMock()
        provider.complete.return_value = {"text": "short answer", "error": None,
                                          "latency_ms": 5.0, "usage": {}}
        evals = MagicMock()
        evals.list_evaluations.return_value = {bm.evaluation_name("lem-simple"): {"id": "e1"}}
        evals.query.return_value = [["a", "lem-simple", "champ", True, "grounded"]]
        with patch.object(bm, "emit_generation", return_value="evt-1"):
            run = bm.run_benchmark(self._suites(), [], {"lem-simple": "champ"},
                                   run_id="bm-1", today="2026-07-27", provider=provider,
                                   evals=evals, judge_cap=10)
        card = run["scorecards"][0]
        assert card["judged"] == 1 and card["judge_pass_rate"] == 1.0
        assert evals.create_evaluation.called is False


# ─────────────────────────────────── the CLI ─────────────────────────────────────

class TestCLI:
    def test_print_suites_validates_the_shipped_fixtures(self, capsys):
        assert bm.main(["--print-suites", "--suite-dir", str(SUITE_DIR)]) == 0
        out = capsys.readouterr().out
        for tier in bm.TIERS:
            assert tier in out

    def test_dry_run_writes_a_report_and_the_leaderboard(self, tmp_path):
        code = bm.main(["--dry-run", "--models", "cand", "--suite-dir", str(SUITE_DIR),
                        "--out-dir", str(tmp_path), "--run-id", "bm-t", "--today", "2026-07-27",
                        "--champions", "lem-simple=champ", "--max-judge-calls", "5"])
        assert code in (0, 2)
        assert (tmp_path / "2026-07-27-bm-t.md").exists()
        assert bm.LEADERBOARD_BEGIN in (tmp_path / "README.md").read_text()

    def test_a_real_run_is_refused_while_the_feature_gate_is_off(self, monkeypatch, capsys):
        monkeypatch.delenv("BENCHMARK_ENABLED", raising=False)
        assert bm.main(["--run", "--suite-dir", str(SUITE_DIR)]) == 0
        assert "BENCHMARK_ENABLED" in capsys.readouterr().out

    def test_a_real_run_without_provider_credentials_exits_1(self, monkeypatch):
        monkeypatch.setenv("BENCHMARK_ENABLED", "true")
        monkeypatch.delenv("OLLAMA_CLOUD_URL", raising=False)
        monkeypatch.delenv("OLLAMA_CLOUD_API_KEY", raising=False)
        assert bm.main(["--run", "--suite-dir", str(SUITE_DIR)]) == 1

    @pytest.mark.parametrize("env,expect_evals", [
        ({"POSTHOG_PERSONAL_API_KEY": "phx", "POSTHOG_API_KEY": "phc"}, True),
        ({"POSTHOG_PERSONAL_API_KEY": "phx"}, False),   # nothing emitted -> nothing to score
        ({"POSTHOG_API_KEY": "phc"}, False),            # no eval API access
        ({}, False),
    ])
    def test_posthog_evaluations_need_both_keys_or_the_fallback_judge_runs(
            self, monkeypatch, tmp_path, env, expect_evals):
        monkeypatch.setenv("BENCHMARK_ENABLED", "true")
        monkeypatch.setenv("OLLAMA_CLOUD_URL", "https://ollama.example")
        monkeypatch.setenv("OLLAMA_CLOUD_API_KEY", "key")
        for name in ("POSTHOG_PERSONAL_API_KEY", "POSTHOG_API_KEY"):
            monkeypatch.delenv(name, raising=False)
        for name, value in env.items():
            monkeypatch.setenv(name, value)
        with patch.object(bm, "run_benchmark") as run_benchmark:
            run_benchmark.return_value = {"recommendations": [], "run_id": "x"}
            with patch.object(bm, "write_report", return_value="p"):
                bm.main(["--run", "--suite-dir", str(SUITE_DIR), "--tiers", "lem-simple",
                         "--out-dir", str(tmp_path)])
        assert (run_benchmark.call_args.kwargs["evals"] is not None) is expect_evals

    def test_render_reproduces_a_saved_run_without_network(self, tmp_path):
        results = tmp_path / "run.json"
        bm.main(["--dry-run", "--suite-dir", str(SUITE_DIR), "--out-dir", str(tmp_path / "a"),
                 "--run-id", "bm-r", "--today", "2026-07-27", "--champions", "lem-simple=champ",
                 "--results-out", str(results), "--max-judge-calls", "3"])
        out = tmp_path / "b"
        assert bm.main(["--render", str(results), "--out-dir", str(out)]) in (0, 2)
        assert (out / "2026-07-27-bm-r.md").exists()

    def test_render_of_a_run_that_measured_nothing_exits_1_and_writes_nothing(self, tmp_path,
                                                                             capsys):
        """#923: the unattended path. A harness-wide failure must not become a PR asserting that
        every model LEM runs scores zero — the cron reads any rc outside {0, 2} as a real failure."""
        results = tmp_path / "outage.json"
        results.write_text(json.dumps(_outage_run([
            _measured_card(model="champ", role="champion"), _measured_card(model="cand")])))
        out = tmp_path / "docs"
        assert bm.main(["--render", str(results), "--out-dir", str(out)]) == 1
        assert not out.exists()
        err = capsys.readouterr().err
        assert err.count("No module named 'openai'") == 1

    def test_render_refuses_a_tainted_results_file(self, tmp_path):
        results = tmp_path / "bad.json"
        results.write_text(json.dumps({"source": "production", "run_id": "x", "date": "d",
                                       "scorecards": []}))
        assert bm.main(["--render", str(results), "--out-dir", str(tmp_path)]) == 1

    def test_recommendations_are_written_separately_for_the_cron(self, tmp_path):
        recs = tmp_path / "recs.json"
        bm.main(["--dry-run", "--models", "cand", "--suite-dir", str(SUITE_DIR),
                 "--out-dir", str(tmp_path), "--run-id", "bm-c", "--today", "2026-07-27",
                 "--recommendations-out", str(recs), "--max-judge-calls", "300"])
        assert isinstance(json.loads(recs.read_text()), list)

    def test_a_dry_run_reads_no_usage_page_but_still_honours_an_override(self, tmp_path):
        bm.main(["--dry-run", "--models", "cand", "--suite-dir", str(SUITE_DIR),
                 "--out-dir", str(tmp_path), "--run-id", "bm-u1", "--today", "2026-07-27",
                 "--champions", "lem-simple=champ", "--tiers", "lem-simple",
                 "--usage-levels", "champ=medium", "--max-judge-calls", "5"])
        report = (tmp_path / "2026-07-27-bm-u1.md").read_text()
        assert "Medium (2)" in report   # the override
        # The candidate's own row, never guessed — the footnote also says "unknown", so assert on
        # the scorecard row rather than on the word appearing anywhere in the page.
        assert "| `cand` | candidate | unknown |" in report

    def test_the_env_supplies_the_levels_when_nobody_is_there_to_pass_the_flag(self, monkeypatch,
                                                                              tmp_path):
        # An unattended run (#842 `1B`) has no human to type `--usage-levels`, and an unsupplied
        # level is a quota RISK, not a free pass — so the runner's env carries the same string.
        monkeypatch.setenv("BENCHMARK_USAGE_LEVELS", "champ=medium")
        bm.main(["--dry-run", "--models", "cand", "--suite-dir", str(SUITE_DIR),
                 "--out-dir", str(tmp_path), "--run-id", "bm-u4", "--today", "2026-07-27",
                 "--champions", "lem-simple=champ", "--tiers", "lem-simple",
                 "--max-judge-calls", "5"])
        assert "Medium (2)" in (tmp_path / "2026-07-27-bm-u4.md").read_text()

    def test_an_explicit_flag_wins_over_the_env(self, monkeypatch, tmp_path):
        monkeypatch.setenv("BENCHMARK_USAGE_LEVELS", "champ=medium")
        bm.main(["--dry-run", "--models", "cand", "--suite-dir", str(SUITE_DIR),
                 "--out-dir", str(tmp_path), "--run-id", "bm-u5", "--today", "2026-07-27",
                 "--champions", "lem-simple=champ", "--tiers", "lem-simple",
                 "--usage-levels", "champ=high", "--max-judge-calls", "5"])
        report = (tmp_path / "2026-07-27-bm-u5.md").read_text()
        assert "High (3)" in report and "Medium (2)" not in report

    def test_a_real_run_fetches_levels_for_candidates_and_champions(self, monkeypatch, tmp_path):
        monkeypatch.setenv("BENCHMARK_ENABLED", "true")
        monkeypatch.setenv("OLLAMA_CLOUD_URL", "https://ollama.example")
        monkeypatch.setenv("OLLAMA_CLOUD_API_KEY", "key")
        seen = []

        def fake_fetch(model):
            seen.append(model)
            return {"level": 3, "label": "high"}

        import model_health_check
        monkeypatch.setattr(model_health_check, "fetch_usage_level", fake_fetch)
        monkeypatch.setattr(bm.ProviderClient, "complete", lambda self, *a, **k: {
            "text": "x", "error": None, "latency_ms": 1.0, "usage": {}})
        bm.main(["--run", "--models", "cand", "--suite-dir", str(SUITE_DIR),
                 "--out-dir", str(tmp_path), "--run-id", "bm-u2", "--today", "2026-07-27",
                 "--champions", "lem-simple=champ", "--tiers", "lem-simple", "--no-judge"])
        assert sorted(seen) == ["cand", "champ"]  # a delta needs BOTH sides

    def test_no_usage_levels_skips_the_fetch_entirely(self, monkeypatch, tmp_path):
        monkeypatch.setenv("BENCHMARK_ENABLED", "true")
        monkeypatch.setenv("OLLAMA_CLOUD_URL", "https://ollama.example")
        monkeypatch.setenv("OLLAMA_CLOUD_API_KEY", "key")
        import model_health_check
        boom = MagicMock(side_effect=AssertionError("should not be called"))
        monkeypatch.setattr(model_health_check, "fetch_usage_level", boom)
        monkeypatch.setattr(bm.ProviderClient, "complete", lambda self, *a, **k: {
            "text": "x", "error": None, "latency_ms": 1.0, "usage": {}})
        bm.main(["--run", "--models", "cand", "--suite-dir", str(SUITE_DIR),
                 "--out-dir", str(tmp_path), "--run-id", "bm-u3", "--today", "2026-07-27",
                 "--champions", "lem-simple=champ", "--tiers", "lem-simple", "--no-judge",
                 "--no-usage-levels"])
        boom.assert_not_called()

    def test_a_bad_suite_directory_exits_1(self, tmp_path, capsys):
        assert bm.main(["--print-suites", "--suite-dir", str(tmp_path)]) == 1
        assert "suite error" in capsys.readouterr().err
