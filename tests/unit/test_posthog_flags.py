"""Unit tests for scripts/posthog_flags.py — the boolean-flag provisioner.

The property under test is the one that can break production: a flag must be created at the rollout
that reproduces what it resolves to TODAY. `flag_enabled()` currently falls back to `env_default()`
everywhere, so a flag created at the wrong percentage changes behaviour the instant it appears — no
deploy, no log line, no error."""

import importlib.util
import pathlib
from unittest.mock import MagicMock

import pytest

pytestmark = pytest.mark.unit

_SCRIPTS = pathlib.Path(__file__).resolve().parents[2] / "scripts"


def _load(name):
    spec = importlib.util.spec_from_file_location(name, _SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


phf = _load("posthog_flags")


def _spec(key, rollout, resolved, env_var="X_ENABLED"):
    return {"key": key, "rollout": rollout, "resolved": resolved, "name": key,
            "env_var": env_var, "owner": "test"}


def _existing(key, rollout, active=True, flag_id=1):
    return {key: {"id": flag_id, "key": key, "active": active,
                  "filters": {"groups": [{"properties": [], "rollout_percentage": rollout}]}}}


# ── the safety property ───────────────────────────────────────────────────────

class TestRolloutMatchesTodaysValue:
    def test_registry_derives_rollout_from_the_resolved_value(self):
        specs = {s["key"]: s for s in phf.registry_specs()}
        for spec in specs.values():
            assert spec["rollout"] == (100 if spec["resolved"] else 0), spec["key"]

    def test_a_default_true_flag_is_provisioned_at_100(self):
        """The live example. Creating this one at the 'obvious' 0% would silently flip the fleet
        engagement default for every user with no saved preferences row."""
        specs = {s["key"]: s for s in phf.registry_specs()}
        fallback = specs["feed-fallback-when-empty-default"]
        assert fallback["resolved"] is True
        assert fallback["rollout"] == 100

    def test_env_override_moves_the_rollout(self, monkeypatch):
        monkeypatch.setenv("COMMENT_RESEARCH_ENABLED", "true")
        from cqc_lem.utilities import flags
        flags.reset_flag_state()
        specs = {s["key"]: s for s in phf.registry_specs()}
        assert specs["comment-research-enabled"]["rollout"] == 100

    def test_every_registry_flag_is_planned(self):
        from cqc_lem.utilities import flags
        assert {s["key"] for s in phf.registry_specs()} == set(flags.FLAGS)


# ── payload shape ─────────────────────────────────────────────────────────────

class TestPayload:
    def test_conditions_are_property_free(self):
        """flags.py evaluates locally; a person-property condition silently falls back to env in
        every Celery worker, which looks exactly like the flag working."""
        payload = phf.flag_payload(_spec("k", 100, True))
        groups = payload["filters"]["groups"]
        assert len(groups) == 1
        assert groups[0]["properties"] == []
        assert groups[0]["rollout_percentage"] == 100

    def test_flag_is_created_active(self):
        assert phf.flag_payload(_spec("k", 0, False))["active"] is True


# ── planning ──────────────────────────────────────────────────────────────────

class TestPlan:
    def test_missing_flag_is_created(self):
        actions = phf.plan_flags([_spec("k", 0, False)], {})
        assert [a["action"] for a in actions] == ["create_flag"]

    def test_matching_flag_is_a_noop(self):
        actions = phf.plan_flags([_spec("k", 100, True)], _existing("k", 100))
        assert [a["action"] for a in actions] == ["noop"]
        assert phf.pending(actions) == []

    def test_rollout_drift_is_an_update(self):
        actions = phf.plan_flags([_spec("k", 100, True)], _existing("k", 0))
        assert actions[0]["action"] == "update_flag"
        assert "rollout" in actions[0]["fields"]

    def test_disabled_flag_is_reactivated(self):
        actions = phf.plan_flags([_spec("k", 0, False)], _existing("k", 0, active=False))
        assert actions[0]["action"] == "update_flag"
        assert "active" in actions[0]["fields"]

    def test_missing_groups_reads_as_drift_not_a_crash(self):
        existing = {"k": {"id": 1, "key": "k", "active": True, "filters": {}}}
        actions = phf.plan_flags([_spec("k", 0, False)], existing)
        assert actions[0]["action"] == "update_flag"


# ── apply ─────────────────────────────────────────────────────────────────────

class TestApply:
    def test_dry_run_writes_nothing(self):
        client = MagicMock()
        log = phf.apply_actions(client, phf.plan_flags([_spec("k", 0, False)], {}), dry_run=True)
        client.create_flag.assert_not_called()
        assert "[dry-run]" in log[0]

    def test_apply_creates(self):
        client = MagicMock()
        client.create_flag.return_value = 42
        phf.apply_actions(client, phf.plan_flags([_spec("k", 0, False)], {}), dry_run=False)
        client.create_flag.assert_called_once()
        assert client.create_flag.call_args[0][0]["key"] == "k"

    def test_apply_updates(self):
        client = MagicMock()
        phf.apply_actions(client, phf.plan_flags([_spec("k", 100, True)], _existing("k", 0)),
                          dry_run=False)
        client.update_flag.assert_called_once()

    def test_dry_run_line_names_the_env_var_behind_the_value(self):
        log = phf.apply_actions(MagicMock(), phf.plan_flags([_spec("k", 100, True, "MY_VAR")], {}),
                                dry_run=True)
        assert "MY_VAR" in log[0] and "100%" in log[0]

    def test_second_run_is_in_sync(self):
        spec = _spec("k", 100, True)
        actions = phf.plan_flags([spec], _existing("k", 100))
        assert phf.summarize(actions).startswith("PostHog feature flags in sync")


class TestCli:
    def test_print_spec_needs_no_key(self, capsys, monkeypatch):
        monkeypatch.delenv("POSTHOG_PERSONAL_API_KEY", raising=False)
        assert phf.main(["--print-spec"]) == 0
        assert "feed-fallback-when-empty-default" in capsys.readouterr().out

    def test_missing_key_is_an_error(self, monkeypatch):
        monkeypatch.delenv("POSTHOG_PERSONAL_API_KEY", raising=False)
        assert phf.main([]) == 1
