"""Unit tests for scripts/posthog_experiments.py (issue #652).

The script is what turns the code's experiment registry into something PostHog can render a readout
from, so the tests pin the two things a wrong payload breaks silently: the ARM LIST (an arm the code
resolves but the flag doesn't define makes every worker fall back to control) and the RAMP (an
`--apply` that reset a live experiment's rollout would re-cohort it mid-run).
"""
import importlib.util
from pathlib import Path
from unittest.mock import MagicMock

import pytest

pytestmark = pytest.mark.unit

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "posthog_experiments.py"


@pytest.fixture(scope="module")
def mod():
    spec = importlib.util.spec_from_file_location("lem_posthog_experiments", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _spec(mod, key):
    return mod.EXPERIMENTS[key]


# ── arm lists ──

def test_flag_assigned_arms_come_straight_from_the_registry(mod):
    spec = _spec(mod, "comment-contract-prompt")
    assert mod.variants_for(spec) == list(spec.variants)
    assert mod.variants_for(spec)[0] == spec.control


def test_shipped_arms_are_derived_from_the_live_combo_matrix(mod):
    """The media experiment's arms are data, not registry text — derived from DEFAULT_COMBOS so they
    cannot drift the first time a combo changes.
    """
    spec = _spec(mod, "post-media-variant")
    arms = mod.variants_for(spec)
    assert arms[0] == spec.control
    assert set(mod.media_combo_arms()) <= set(arms)
    assert len(arms) == len(set(arms)), "duplicate combos must collapse to one arm"


# ── rollout maths ──

@pytest.mark.parametrize("key", ["cost-routing-arm", "comment-contract-prompt",
                                 "post-media-variant"])
def test_rollout_percentages_always_sum_to_exactly_100(mod, key):
    """PostHog rejects a multivariate flag whose variants don't total 100."""
    spec = _spec(mod, key)
    rows = mod.rollout_percentages(spec, mod.variants_for(spec))
    assert sum(row["rollout_percentage"] for row in rows) == 100
    assert rows[0]["key"] == spec.control
    assert all(isinstance(row["rollout_percentage"], int) for row in rows)


def test_the_remainder_is_parked_in_control_not_distributed(mod):
    spec = _spec(mod, "post-media-variant")
    arms = mod.variants_for(spec)
    rows = {row["key"]: row["rollout_percentage"] for row in mod.rollout_percentages(spec, arms)}
    treatments = [key for key in rows if key != spec.control]
    assert len({rows[key] for key in treatments}) == 1  # even split across treatments
    assert rows[spec.control] == 100 - sum(rows[key] for key in treatments)


def test_flag_payload_uses_a_property_free_release_condition(mod):
    """Local evaluation cannot resolve a condition that needs server-held person properties, and one
    it cannot resolve makes every Celery worker silently read the control arm.
    """
    payload = mod.flag_payload(_spec(mod, "cost-routing-arm"))
    groups = payload["filters"]["groups"]
    assert groups == [{"properties": [], "rollout_percentage": 100}]
    assert payload["key"] == "cost-routing-arm"
    assert payload["active"] is True


def test_experiment_payload_measures_the_events_lem_already_emits(mod):
    spec = _spec(mod, "cost-routing-arm")
    payload = mod.experiment_payload(spec, flag_id=11)
    assert payload["feature_flag_key"] == spec.key
    assert payload["feature_flag"] == 11
    events = [metric["source"]["event"] for metric in payload["metrics"]]
    assert events == list(spec.metric_events)


# ── planning ──

def test_plan_creates_the_flag_then_blocks_the_experiment_on_it(mod):
    actions = mod.plan_actions(mod.build_specs(), {}, {})
    kinds = [a["action"] for a in actions]
    assert kinds.count("create_flag") == len(mod.EXPERIMENTS)
    # PostHog needs the flag id, so promising both in one dry-run pass would be a lie.
    assert kinds.count("blocked_experiment") == len(mod.EXPERIMENTS)
    assert mod.pending(actions)


def test_plan_is_clean_when_flags_and_experiments_already_match(mod):
    flags = {spec.key: {"id": i, "active": True, "variants": mod.variants_for(spec)}
             for i, spec in enumerate(mod.build_specs())}
    experiments = {spec.key: {"id": 100 + i} for i, spec in enumerate(mod.build_specs())}
    actions = mod.plan_actions(mod.build_specs(), flags, experiments)
    assert {a["action"] for a in actions} == {"unchanged_flag", "unchanged_experiment"}
    assert mod.pending(actions) == []


def test_plan_repairs_a_flag_missing_an_arm_the_code_resolves(mod):
    spec = _spec(mod, "comment-contract-prompt")
    flags = {spec.key: {"id": 3, "active": True, "variants": [spec.control]}}
    actions = mod.plan_actions([spec], flags, {spec.key: {"id": 9}})
    repair = next(a for a in actions if a["action"] == "update_flag_variants")
    assert repair["missing"] == ["author-question"]
    assert repair["flag_id"] == 3


def test_plan_reactivates_a_disabled_flag(mod):
    spec = _spec(mod, "comment-contract-prompt")
    flags = {spec.key: {"id": 3, "active": False, "variants": mod.variants_for(spec)}}
    actions = mod.plan_actions([spec], flags, {spec.key: {"id": 9}})
    assert [a["action"] for a in actions] == ["update_flag_variants", "unchanged_experiment"]


def test_an_existing_rollout_is_never_planned_as_drift(mod):
    """PostHog owns the ramp once the experiment runs — an --apply that reset a 50% ramp to the
    spec's 10% start would silently re-cohort a live experiment.
    """
    spec = _spec(mod, "cost-routing-arm")
    flags = {spec.key: {"id": 3, "active": True, "variants": mod.variants_for(spec)}}
    actions = mod.plan_actions([spec], flags, {spec.key: {"id": 9}})
    assert [a["action"] for a in actions] == ["unchanged_flag", "unchanged_experiment"]


def test_explicit_rollout_is_planned_and_re_splits_the_variants(mod):
    spec = _spec(mod, "cost-routing-arm")
    flags = {spec.key: {"id": 3, "active": True, "variants": mod.variants_for(spec)}}
    actions = mod.plan_actions([spec], flags, {spec.key: {"id": 9}}, rollouts={spec.key: 0.5})
    rollout = next(a for a in actions if a["action"] == "set_rollout")
    variants = {v["key"]: v["rollout_percentage"]
                for v in rollout["payload"]["filters"]["multivariate"]["variants"]}
    assert variants == {"control": 50, "treatment": 50}
    assert mod.pending(actions)


def test_creates_the_experiment_when_the_flag_already_exists(mod):
    spec = _spec(mod, "cost-routing-arm")
    flags = {spec.key: {"id": 3, "active": True, "variants": mod.variants_for(spec)}}
    actions = mod.plan_actions([spec], flags, {})
    create = next(a for a in actions if a["action"] == "create_experiment")
    assert create["payload"]["feature_flag"] == 3


# ── CLI parsing ──

def test_parse_rollout_accepts_a_percentage(mod):
    assert mod.parse_rollout("cost-routing-arm=25") == {"cost-routing-arm": 0.25}


@pytest.mark.parametrize("expression", ["nope=10", "cost-routing-arm=abc",
                                        "cost-routing-arm=101", "cost-routing-arm=-1"])
def test_parse_rollout_rejects_typos_instead_of_no_opping(mod, expression):
    with pytest.raises(ValueError):
        mod.parse_rollout(expression)


# ── apply ──

def test_dry_run_writes_nothing(mod):
    client = MagicMock()
    log = mod.apply_actions(client, mod.plan_actions(mod.build_specs(), {}, {}), dry_run=True)
    assert log and all(line.startswith("[dry-run]") or line.startswith("skipped")
                       for line in log)
    client.create_flag.assert_not_called()
    client.create_experiment.assert_not_called()
    client.update_flag.assert_not_called()


def test_apply_creates_the_flag_and_reuses_its_id_for_the_experiment(mod):
    spec = _spec(mod, "cost-routing-arm")
    client = MagicMock()
    client.create_flag.return_value = 42
    actions = [{"action": "create_flag", "flag": spec.key, "payload": mod.flag_payload(spec)},
               {"action": "create_experiment", "experiment": spec.key,
                "payload": mod.experiment_payload(spec)}]
    mod.apply_actions(client, actions, dry_run=False)
    client.create_flag.assert_called_once()
    assert client.create_experiment.call_args.args[0]["feature_flag"] == 42


def test_apply_skips_a_rollout_for_a_flag_that_does_not_exist_yet(mod):
    spec = _spec(mod, "cost-routing-arm")
    client = MagicMock()
    log = mod.apply_actions(client, [{"action": "set_rollout", "flag": spec.key, "flag_id": None,
                                      "rollout_pct": 0.5,
                                      "payload": mod.rollout_payload(spec, 0.5)}], dry_run=False)
    client.update_flag.assert_not_called()
    assert "does not exist yet" in log[0]


def test_summarize_counts_every_action_kind(mod):
    summary = mod.summarize(mod.plan_actions(mod.build_specs(), {}, {}))
    assert "create_flag=" in summary and "blocked_experiment=" in summary


def test_experiment_url_points_at_the_project(mod):
    assert mod.experiment_url("https://us.posthog.com/", "475262", 7) == \
        "https://us.posthog.com/project/475262/experiments/7"


class TestMainKeyGate:
    def test_missing_key_is_an_error(self, mod, monkeypatch, capsys):
        monkeypatch.delenv("POSTHOG_PERSONAL_API_KEY", raising=False)
        monkeypatch.delenv("POSTHOG_OPERATOR_API_KEY", raising=False)
        assert mod.main([]) == 1
        assert "POSTHOG_PERSONAL_API_KEY" in capsys.readouterr().err

    def test_operator_key_alone_reaches_the_client(self, mod, monkeypatch, capsys):
        # issue #1453 follow-up: this hand-run script reads POSTHOG_OPERATOR_API_KEY, not the
        # shared POSTHOG_PERSONAL_API_KEY directly.
        monkeypatch.delenv("POSTHOG_PERSONAL_API_KEY", raising=False)
        monkeypatch.setenv("POSTHOG_OPERATOR_API_KEY", "phx_operator")
        captured = {}

        class _Stub:
            def __init__(self, api_key, project_id, app_host):
                captured["api_key"] = api_key

            def list_flags(self):
                raise RuntimeError("stop after the key gate")

        monkeypatch.setattr(mod, "PostHogClient", _Stub)
        assert mod.main([]) == 1
        assert captured["api_key"] == "phx_operator"
        assert "Failed to read PostHog state" in capsys.readouterr().err
