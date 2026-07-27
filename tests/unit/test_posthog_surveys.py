"""Unit tests for scripts/posthog_surveys.py — the NPS/CSAT survey specs, the targeting rules and
the create/update planner (issue #653)."""

import copy
import importlib.util
import pathlib
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

pytestmark = pytest.mark.unit

_SCRIPTS = pathlib.Path(__file__).resolve().parents[2] / "scripts"


def _load(name):
    spec = importlib.util.spec_from_file_location(name, _SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


phs = _load("posthog_surveys")


def _spec(name):
    return next(s for s in phs.build_specs() if s["name"] == name)


def _live(spec, **overrides):
    """What PostHog would return for a survey created from `spec` — the shape drift is measured
    against, including the targeting flag echoed back under its own key. Deep-copied: a test that
    edits the "live" side must not edit the spec it is being compared to."""
    spec = copy.deepcopy(spec)
    live = {k: v for k, v in spec.items() if k != "targeting_flag_filters"}
    live.update({"id": "0199-abc", "archived": False, "start_date": None,
                 "targeting_flag": {"filters": spec.get("targeting_flag_filters")}})
    live.update(overrides)
    return live


class TestSpecs:
    def test_both_surveys_are_defined(self):
        assert [s["name"] for s in phs.build_specs()] == [phs.NPS_SURVEY_NAME,
                                                          phs.CSAT_SURVEY_NAME]

    def test_survey_names_are_unique(self):
        # plan_actions keys the live set by name; a duplicate would make two specs fight over one
        # survey forever.
        names = [s["name"] for s in phs.build_specs()]
        assert len(names) == len(set(names))

    def test_both_are_api_surveys_so_the_spa_renders_them(self):
        # A popover survey's answer is a PostHog event and nothing else — it would never reach the
        # feedback->auto-work loop, which is the whole reason LEM asks.
        assert all(s["type"] == "api" for s in phs.build_specs())

    def test_every_survey_leads_with_a_rating_and_ends_with_an_optional_open_question(self):
        for spec in phs.build_specs():
            questions = spec["questions"]
            assert questions[0]["type"] == "rating"
            assert questions[0]["optional"] is False
            assert questions[-1]["type"] == "open"
            # A required "why" turns a 5-second ask into an abandoned one.
            assert questions[-1]["optional"] is True

    def test_nps_uses_the_zero_to_ten_band_and_csat_a_five_point_scale(self):
        assert _spec(phs.NPS_SURVEY_NAME)["questions"][0]["scale"] == 10
        assert _spec(phs.CSAT_SURVEY_NAME)["questions"][0]["scale"] == 5

    def test_rating_scales_are_ones_posthog_supports(self):
        for spec in phs.build_specs():
            assert spec["questions"][0]["scale"] in (2, 3, 5, 7, 10)
            assert spec["questions"][0]["display"] in ("number", "emoji")

    def test_every_survey_carries_the_cross_survey_wait_period(self):
        # Answering or dismissing either survey buys silence from BOTH — that is the throttle the
        # "no double-prompt" promise leans on.
        for spec in phs.build_specs():
            assert spec["conditions"]["seenSurveyWaitPeriodInDays"] == phs.SURVEY_WAIT_DAYS

    def test_nps_targets_thirty_days_past_activation_not_signup(self):
        groups = _spec(phs.NPS_SURVEY_NAME)["targeting_flag_filters"]["groups"]
        prop = groups[0]["properties"][0]
        assert prop["key"] == phs.PERSON_ONBOARDING_COMPLETED
        assert prop["type"] == "person"
        assert prop["operator"] == "is_date_before"
        assert prop["value"] == f"-{phs.NPS_AFTER_ACTIVATION_DAYS}d"
        assert groups[0]["rollout_percentage"] == 100

    def test_csat_is_triggered_by_an_approval_and_gated_on_the_approval_count(self):
        spec = _spec(phs.CSAT_SURVEY_NAME)
        events = spec["conditions"]["events"]
        assert [e["name"] for e in events["values"]] == [phs.CSAT_TRIGGER_EVENT]
        assert events["repeatedActivation"] is True
        prop = spec["targeting_flag_filters"]["groups"][0]["properties"][0]
        assert prop["key"] == phs.PERSON_POSTS_APPROVED
        assert prop["operator"] == "gte"
        assert prop["value"] == phs.CSAT_MIN_APPROVALS

    def test_nps_has_no_event_trigger(self):
        # NPS is a time-based ask; an event trigger would make it fire on whatever the user happened
        # to be doing at day 30.
        assert "events" not in _spec(phs.NPS_SURVEY_NAME)["conditions"]

    def test_descriptions_are_present_and_fit_the_posthog_limit(self):
        for spec in phs.build_specs():
            assert spec["description"]
            assert len(spec["description"]) <= 400


class TestPayloads:
    def test_the_create_payload_never_launches_the_survey(self):
        # A survey created by --apply must not start collecting responses from a definition nobody
        # has read; --launch is the explicit second step.
        payload = phs.create_payload(_spec(phs.NPS_SURVEY_NAME))
        assert "start_date" not in payload
        assert payload["enable_partial_responses"] is False
        assert payload["type"] == "api"

    def test_the_create_payload_carries_the_targeting_filters(self):
        payload = phs.create_payload(_spec(phs.CSAT_SURVEY_NAME))
        assert payload["targeting_flag_filters"]["groups"][0]["properties"]

    def test_the_update_payload_keeps_targeting_so_a_tuned_rule_actually_lands(self):
        assert "targeting_flag_filters" in phs.update_payload(_spec(phs.NPS_SURVEY_NAME))


class TestDrift:
    def test_an_untouched_survey_reports_no_drift(self):
        spec = _spec(phs.NPS_SURVEY_NAME)
        assert phs.drifted_fields(spec, _live(spec)) == []

    def test_posthog_stringifying_a_number_is_not_drift(self):
        # PostHog echoes property-filter values back as strings; comparing JSON text would report
        # drift on every single run.
        spec = _spec(phs.CSAT_SURVEY_NAME)
        live = _live(spec)
        live["targeting_flag"]["filters"]["groups"][0]["properties"][0]["value"] = "5"
        assert phs.drifted_fields(spec, live) == []

    def test_extra_flag_machinery_posthog_adds_is_not_drift(self):
        spec = _spec(phs.NPS_SURVEY_NAME)
        live = _live(spec)
        live["targeting_flag"]["filters"]["multivariate"] = None
        live["targeting_flag"]["filters"]["payloads"] = {}
        live["created_by"] = {"id": 7}
        assert phs.drifted_fields(spec, live) == []

    def test_a_changed_question_is_drift(self):
        spec = _spec(phs.NPS_SURVEY_NAME)
        live = _live(spec)
        live["questions"] = [dict(spec["questions"][0], question="Rate us")]
        assert "questions" in phs.drifted_fields(spec, live)

    def test_a_removed_wait_period_is_drift(self):
        spec = _spec(phs.CSAT_SURVEY_NAME)
        live = _live(spec)
        live["conditions"] = {"events": spec["conditions"]["events"]}
        assert "conditions" in phs.drifted_fields(spec, live)

    def test_a_missing_targeting_flag_is_drift(self):
        spec = _spec(phs.NPS_SURVEY_NAME)
        live = _live(spec)
        live["targeting_flag"] = None
        assert "targeting_flag_filters" in phs.drifted_fields(spec, live)

    def test_a_widened_rollout_is_drift(self):
        spec = _spec(phs.NPS_SURVEY_NAME)
        live = _live(spec)
        live["targeting_flag"]["filters"]["groups"][0]["rollout_percentage"] = 10
        assert "targeting_flag_filters" in phs.drifted_fields(spec, live)


class TestPlan:
    def test_missing_surveys_are_created(self):
        actions = phs.plan_actions(phs.build_specs(), {})
        assert [a["action"] for a in actions] == ["create_survey", "create_survey"]

    def test_an_in_sync_survey_is_a_noop(self):
        specs = phs.build_specs()
        existing = {s["name"]: _live(s) for s in specs}
        assert {a["action"] for a in phs.plan_actions(specs, existing)} == {"noop"}

    def test_a_drifted_survey_is_updated_and_names_the_fields(self):
        specs = phs.build_specs()
        existing = {s["name"]: _live(s) for s in specs}
        existing[phs.NPS_SURVEY_NAME]["description"] = "edited in the UI"
        actions = phs.plan_actions(specs, existing)
        update = next(a for a in actions if a["action"] == "update_survey")
        assert update["survey"] == phs.NPS_SURVEY_NAME
        assert update["fields"] == ["description"]
        assert update["survey_id"] == "0199-abc"

    def test_launch_is_only_planned_for_a_survey_that_never_started(self):
        specs = phs.build_specs()
        existing = {s["name"]: _live(s) for s in specs}
        existing[phs.CSAT_SURVEY_NAME]["start_date"] = "2026-07-01T00:00:00Z"
        actions = phs.plan_actions(specs, existing, launch=True)
        launched = [a["survey"] for a in actions if a["action"] == "launch_survey"]
        assert launched == [phs.NPS_SURVEY_NAME]

    def test_nothing_launches_without_the_flag(self):
        specs = phs.build_specs()
        existing = {s["name"]: _live(s) for s in specs}
        assert not [a for a in phs.plan_actions(specs, existing) if a["action"] == "launch_survey"]

    def test_pending_and_summary_read_the_plan(self):
        assert phs.summarize(phs.plan_actions(phs.build_specs(), {})).startswith("Pending:")
        specs = phs.build_specs()
        in_sync = phs.plan_actions(specs, {s["name"]: _live(s) for s in specs})
        assert phs.pending(in_sync) == []
        assert "in sync" in phs.summarize(in_sync)


class TestApply:
    def test_dry_run_writes_nothing(self):
        client = MagicMock()
        log = phs.apply_actions(client, phs.plan_actions(phs.build_specs(), {}), dry_run=True)
        client.create_survey.assert_not_called()
        assert all(line.startswith("[dry-run]") for line in log)

    def test_apply_creates_updates_and_launches(self):
        client = MagicMock()
        client.create_survey.return_value = "0199-new"
        specs = phs.build_specs()
        existing = {specs[1]["name"]: _live(specs[1], description="edited")}
        actions = phs.plan_actions(specs, existing, launch=True)
        now = datetime(2026, 7, 27, 12, 0, tzinfo=timezone.utc)
        phs.apply_actions(client, actions, dry_run=False, now=now)

        client.create_survey.assert_called_once()
        assert client.create_survey.call_args.args[0]["name"] == phs.NPS_SURVEY_NAME
        client.update_survey.assert_called_once()
        client.launch_survey.assert_called_once_with("0199-abc", now.isoformat())

    def test_an_archived_survey_is_invisible_to_the_planner(self):
        # list_surveys drops archived rows, so an archived survey re-creates rather than being
        # patched back to life under a name the SPA is matching on.
        client = phs.PostHogSurveyClient("key", "1")
        client._paged = lambda path: [{"name": phs.NPS_SURVEY_NAME, "archived": True},
                                      {"name": phs.CSAT_SURVEY_NAME, "archived": False, "id": 2}]
        assert set(client.list_surveys()) == {phs.CSAT_SURVEY_NAME}


class TestCli:
    def test_print_spec_needs_no_network(self, capsys):
        assert phs.main(["--print-spec"]) == 0
        assert phs.NPS_SURVEY_NAME in capsys.readouterr().out

    def test_missing_api_key_is_an_error(self, monkeypatch, capsys):
        monkeypatch.delenv("POSTHOG_PERSONAL_API_KEY", raising=False)
        assert phs.main([]) == 1
        assert "POSTHOG_PERSONAL_API_KEY" in capsys.readouterr().err

    def test_dry_run_exits_2_when_changes_are_pending(self, monkeypatch, capsys):
        monkeypatch.setenv("POSTHOG_PERSONAL_API_KEY", "phx_test")
        monkeypatch.setattr(phs.PostHogSurveyClient, "list_surveys", lambda self: {})
        assert phs.main(["--dry-run"]) == 2
        assert "[dry-run] create survey" in capsys.readouterr().out

    def test_dry_run_exits_0_when_in_sync(self, monkeypatch):
        monkeypatch.setenv("POSTHOG_PERSONAL_API_KEY", "phx_test")
        monkeypatch.setattr(phs.PostHogSurveyClient, "list_surveys",
                            lambda self: {s["name"]: _live(s) for s in phs.build_specs()})
        assert phs.main(["--dry-run"]) == 0

    def test_an_unreadable_project_is_an_error_not_a_blind_create(self, monkeypatch, capsys):
        monkeypatch.setenv("POSTHOG_PERSONAL_API_KEY", "phx_test")

        def boom(self):
            raise RuntimeError("401")
        monkeypatch.setattr(phs.PostHogSurveyClient, "list_surveys", boom)
        assert phs.main(["--apply"]) == 1
        assert "Failed to read PostHog surveys" in capsys.readouterr().err
