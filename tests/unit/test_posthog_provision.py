"""Unit tests for scripts/posthog_provision.py — the Health/Growth specs, the funnels, the alert
thresholds, the weekly subscription and the create/update planner (issue #650).
"""

import importlib.util
import pathlib
import re
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


php = _load("posthog_provision")


def _all_tiles():
    return [(spec["name"], tile) for spec in php.build_specs() for tile in spec["tiles"]]


def _hogql_tiles():
    return [(dash, tile) for dash, tile in _all_tiles()
            if tile["query"]["kind"] == "DataVisualizationNode"]


def _sql(tile):
    return tile["query"]["source"]["query"]


def _tile_by_name(name):
    return next(tile for _, tile in _all_tiles() if tile["name"] == name)


class TestSpecs:
    def test_three_consolidated_dashboards(self):
        names = [spec["name"] for spec in php.build_specs()]
        assert names == [php.DASH_HEALTH, php.DASH_GROWTH, php.DASH_CHANNELS]
        assert all(spec["tiles"] for spec in php.build_specs())

    def test_tile_names_are_globally_unique(self):
        # plan_actions() keys existing insights by name, so a duplicate would make two tiles fight
        # over one insight forever.
        names = [tile["name"] for _, tile in _all_tiles()]
        assert len(names) == len(set(names))

    def test_no_name_collision_with_the_cost_dashboards_script(self):
        # Both provisioners plan by insight NAME against the same project. A shared name would make
        # each one re-point the other's insight at its own dashboard on every run.
        other = _load("posthog_dashboards")
        theirs = {tile["name"] for spec in other.build_specs() for tile in spec["tiles"]}
        ours = {tile["name"] for _, tile in _all_tiles()}
        assert not (ours & theirs)
        assert not ({spec["name"] for spec in php.build_specs()}
                    & {spec["name"] for spec in other.build_specs()})

    def test_every_tile_is_a_known_query_node(self):
        for dashboard, tile in _all_tiles():
            query = tile["query"]
            assert query["kind"] in ("DataVisualizationNode", "InsightVizNode"), dashboard
            assert query["source"]["kind"] in ("HogQLQuery", "TrendsQuery", "FunnelsQuery")
            assert tile["description"]

    def test_descriptions_fit_the_posthog_limit(self):
        for _, tile in _all_tiles():
            assert len(tile["description"]) <= 400, tile["name"]
        for spec in php.build_specs():
            assert len(spec["description"]) <= 400

    def test_hogql_tiles_only_read_known_lem_events(self):
        for dashboard, tile in _hogql_tiles():
            referenced = set(re.findall(r"event (?:=|IN) \(?'([a-z_]+)'", _sql(tile)))
            unknown = referenced - set(php.SOURCE_EVENTS)
            assert not unknown, f"{dashboard}/{tile['name']} reads unknown events {unknown}"

    def test_query_node_tiles_only_read_known_lem_events(self):
        for dashboard, tile in _all_tiles():
            if tile["query"]["kind"] != "InsightVizNode":
                continue
            events = {s["event"] for s in tile["query"]["source"]["series"]}
            assert events <= set(php.SOURCE_EVENTS), f"{dashboard}/{tile['name']}"

    def test_no_aggregate_is_aliased_to_a_column_it_reads(self):
        # ClickHouse rejects `sum(followers) AS followers` outright ("aggregate function is found
        # inside another aggregate function") — the alias re-binds the name the other aggregates in
        # the same SELECT read, so the whole tile renders an error, not a wrong number.
        for dashboard, tile in _hogql_tiles():
            collisions = [(fn, col) for fn, col, alias in re.findall(
                r"\b(sum|count|avg|min|max|median|uniq|uniqExact|any)\s*\(\s*(\w+)\s*\)\s+AS\s+(\w+)",
                _sql(tile)) if col == alias]
            assert not collisions, f"{dashboard}/{tile['name']} self-aliases {collisions}"

    def test_hogql_tiles_start_with_a_select(self):
        for _, tile in _hogql_tiles():
            assert _sql(tile).startswith(("SELECT", "WITH"))

    def test_chart_axes_reference_columns_the_query_selects(self):
        for dashboard, tile in _hogql_tiles():
            settings = tile["query"].get("chartSettings")
            if not settings:
                continue
            aliases = set(re.findall(r"AS (\w+)", _sql(tile)))
            columns = [settings["xAxis"]["column"]] + [y["column"] for y in settings["yAxis"]]
            missing = [c for c in columns if c not in aliases]
            assert not missing, f"{dashboard}/{tile['name']} plots missing columns {missing}"

    def test_funnels_are_ordered_and_have_a_conversion_window(self):
        funnels = [tile for _, tile in _all_tiles()
                   if tile["query"]["source"]["kind"] == "FunnelsQuery"]
        assert len(funnels) == 3
        for tile in funnels:
            source = tile["query"]["source"]
            assert len(source["series"]) >= 3
            assert source["funnelsFilter"]["funnelOrderType"] == "ordered"
            assert source["funnelsFilter"]["funnelWindowInterval"] > 0
            assert source["funnelsFilter"]["funnelWindowIntervalUnit"] == "day"

    def test_content_loop_funnel_follows_the_plan_to_engagement_path(self):
        steps = [s["event"] for s in
                 _tile_by_name("Growth — Content loop funnel")["query"]["source"]["series"]]
        assert steps == ["content_plan_generated", "post_approved", "post_outcome"]

    def test_signup_funnel_ends_at_subscription(self):
        steps = [s["event"] for s in _tile_by_name(
            "Growth — Signup → activation → subscription funnel")["query"]["source"]["series"]]
        assert steps[0] == "signup_started" and steps[-1] == "subscription_started"

    def test_celery_failure_tile_filters_on_the_state_string_not_the_boolean(self):
        # A boolean property filter that doesn't match silently counts nothing — and an alert that
        # never fires is worse than no alert.
        series = _tile_by_name(php.INSIGHT_CELERY_FAILURES)["query"]["source"]["series"][0]
        assert series["properties"] == [
            {"key": "state", "value": ["FAILURE"], "operator": "exact", "type": "event"}]

    def test_llm_spend_reads_the_proxy_cost_not_the_app_estimate(self):
        series = _tile_by_name(php.INSIGHT_LLM_SPEND)["query"]["source"]["series"][0]
        assert series["event"] == "$ai_generation"
        assert (series["math"], series["math_property"]) == ("sum", "$ai_total_cost_usd")
        # Summing both streams double-counts every call (docs/llm-analytics.md), so no tile query
        # may read the app-side estimate.
        assert "llm_call" not in str([tile["query"] for _, tile in _all_tiles()])

    def test_comment_floor_tile_is_weekly(self):
        # Human pacing takes account-wide rest days, so a daily floor would page on a healthy day.
        source = _tile_by_name(php.INSIGHT_COMMENTS_WEEKLY)["query"]["source"]
        assert source["interval"] == "week"

    def test_publish_tile_counts_first_readings_only(self):
        sql = _sql(_tile_by_name("Health — Posts published per day"))
        assert "min(timestamp)" in sql and "GROUP BY post_id" in sql

    def test_follower_delta_drops_the_first_reading(self):
        sql = _sql(_tile_by_name("Health — Follower delta per day"))
        assert "lagInFrame" in sql and "WHERE prev > 0" in sql

    def test_demotion_tile_excludes_unreadable_readings_from_the_denominator(self):
        sql = _sql(_tile_by_name("Growth — Comment visibility (Most-relevant demotion)"))
        assert "visible_most_relevant IS NOT NULL" in sql


class TestAlertSpecs:
    def test_within_the_free_alert_budget(self):
        assert 0 < len(php.alert_specs()) <= 5

    def test_every_alert_points_at_a_tile_that_exists_and_is_alertable(self):
        by_name = {tile["name"]: tile for _, tile in _all_tiles()}
        for spec in php.alert_specs():
            tile = by_name.get(spec["insight"])
            assert tile is not None, spec["insight"]
            source = tile["query"]["source"]
            # An alert threshold is evaluated against ONE series by index — a breakdown or a second
            # series makes `series_index: 0` ambiguous.
            assert source["kind"] == "TrendsQuery"
            assert len(source["series"]) == 1
            assert "breakdownFilter" not in source

    def test_alert_names_are_unique(self):
        names = [spec["name"] for spec in php.alert_specs()]
        assert len(names) == len(set(names))

    def test_every_alert_has_exactly_one_meaningful_bound(self):
        for spec in php.alert_specs():
            bounds = spec["bounds"]
            assert set(bounds) <= {"lower", "upper"} and bounds

    def test_payload_matches_posthogs_alert_shape(self):
        payload = php.alert_payload(php.alert_specs()[0], 42, [7])
        assert payload["insight"] == 42
        assert payload["subscribed_users"] == [7]
        assert payload["config"] == {"type": "TrendsAlertConfig", "series_index": 0}
        assert payload["threshold"]["configuration"]["type"] == "absolute"
        assert payload["condition"] == {"type": "absolute_value"}
        assert payload["enabled"] is True


class TestEvaluateThreshold:
    def test_below_floor_breaches(self):
        verdict = php.evaluate_threshold({"lower": php.COMMENT_WEEKLY_FLOOR}, 0)
        assert verdict["breach"] is True and "below" in verdict["reason"]

    def test_at_the_floor_is_not_a_breach(self):
        assert php.evaluate_threshold({"lower": 5}, 5)["breach"] is False

    def test_above_cap_breaches(self):
        verdict = php.evaluate_threshold({"upper": php.LLM_DAILY_COST_CAP_USD}, 99.5)
        assert verdict["breach"] is True and "above" in verdict["reason"]

    def test_within_bounds_is_quiet(self):
        assert php.evaluate_threshold({"upper": 25}, 3)["breach"] is False

    def test_missing_reading_is_not_a_breach(self):
        # A gap in ingest is not an incident — paging on it would make every quiet hour an alert.
        assert php.evaluate_threshold({"lower": 5}, None)["breach"] is False
        assert php.evaluate_threshold({"lower": 5}, "")["breach"] is False

    def test_string_values_are_compared_numerically(self):
        assert php.evaluate_threshold({"upper": 5}, "6")["breach"] is True


class TestReportSchedule:
    def test_next_start_is_the_coming_monday_at_the_report_hour(self):
        # 2026-07-26 is a Sunday.
        start = php.next_report_start(datetime(2026, 7, 26, 12, 0, tzinfo=timezone.utc))
        assert start == "2026-07-27T09:00:00Z"

    def test_a_monday_before_the_hour_still_schedules_that_day(self):
        start = php.next_report_start(datetime(2026, 7, 27, 3, 0, tzinfo=timezone.utc))
        assert start == "2026-07-27T09:00:00Z"

    def test_a_monday_after_the_hour_rolls_to_next_week(self):
        start = php.next_report_start(datetime(2026, 7, 27, 10, 0, tzinfo=timezone.utc))
        assert start == "2026-08-03T09:00:00Z"

    def test_subscription_spec_targets_the_growth_dashboard_weekly(self):
        spec = php.subscription_spec("owner@example.com",
                                     now=datetime(2026, 7, 26, tzinfo=timezone.utc))
        assert spec["dashboard"] == php.DASH_GROWTH
        assert spec["target_type"] == "email"
        assert spec["target_value"] == "owner@example.com"
        assert spec["frequency"] == "weekly" and spec["byweekday"] == ["monday"]


class TestPlanActions:
    def test_empty_project_creates_everything(self):
        actions = php.plan_actions(php.build_specs(), {}, {})
        assert sum(1 for a in actions if a["action"] == "create_dashboard") == 3
        assert all(a["action"] in ("create_dashboard", "create_insight") for a in actions)

    def test_matching_state_is_unchanged(self):
        specs = php.build_specs()
        dashboards = {spec["name"]: i for i, spec in enumerate(specs, start=1)}
        insights = {}
        for i, spec in enumerate(specs, start=1):
            for tile in spec["tiles"]:
                insights[tile["name"]] = {"id": 900 + len(insights), "query": tile["query"],
                                          "description": tile["description"], "dashboards": [i]}
        actions = php.plan_actions(specs, dashboards, insights)
        assert {a["action"] for a in actions} == {"unchanged"}
        assert php.pending(actions) == []

    def test_drifted_query_is_updated(self):
        specs = php.build_specs()[:1]
        tile = specs[0]["tiles"][0]
        actions = php.plan_actions(
            specs, {specs[0]["name"]: 1},
            {tile["name"]: {"id": 5, "query": {"kind": "edited-away"},
                            "description": tile["description"], "dashboards": [1]}})
        update = next(a for a in actions if a["insight"] == tile["name"])
        assert update["action"] == "update_insight" and update["insight_id"] == 5

    def test_detached_insight_is_reattached(self):
        specs = php.build_specs()[:1]
        tile = specs[0]["tiles"][0]
        actions = php.plan_actions(
            specs, {specs[0]["name"]: 1},
            {tile["name"]: {"id": 5, "query": tile["query"],
                            "description": tile["description"], "dashboards": []}})
        assert next(a for a in actions if a["insight"] == tile["name"])["action"] == "update_insight"

    def test_description_drift_is_updated(self):
        specs = php.build_specs()[:1]
        tile = specs[0]["tiles"][0]
        actions = php.plan_actions(
            specs, {specs[0]["name"]: 1},
            {tile["name"]: {"id": 5, "query": tile["query"], "description": "stale",
                            "dashboards": [1]}})
        assert next(a for a in actions if a["insight"] == tile["name"])["action"] == "update_insight"


class TestPlanAlerts:
    def _insight_ids(self):
        return {spec["insight"]: i for i, spec in enumerate(php.alert_specs(), start=100)}

    def test_missing_insight_blocks_rather_than_creates(self):
        actions = php.plan_alerts(php.alert_specs(), {}, {})
        assert {a["action"] for a in actions} == {"blocked_alert"}
        assert php.pending(actions) == actions

    def test_creates_when_the_insight_exists(self):
        actions = php.plan_alerts(php.alert_specs(), {}, self._insight_ids(), [7])
        assert {a["action"] for a in actions} == {"create_alert"}
        assert actions[0]["payload"]["subscribed_users"] == [7]

    def test_unchanged_when_bounds_and_insight_match(self):
        ids = self._insight_ids()
        existing = {spec["name"]: {"id": 1, "insight_id": ids[spec["insight"]],
                                   "bounds": spec["bounds"], "enabled": True}
                    for spec in php.alert_specs()}
        actions = php.plan_alerts(php.alert_specs(), existing, ids)
        assert {a["action"] for a in actions} == {"unchanged_alert"}

    def test_a_subscriberless_alert_is_repaired(self):
        # An alert PostHog created with no subscribed_users emails nobody. Matching only on bounds
        # would call it healthy forever, so the run that CAN name the owner adds them.
        ids = self._insight_ids()
        spec = php.alert_specs()[0]
        existing = {spec["name"]: {"id": 1, "insight_id": ids[spec["insight"]],
                                   "bounds": spec["bounds"], "enabled": True,
                                   "subscribed_users": []}}
        action = php.plan_alerts([spec], existing, ids, [7])[0]
        assert action["action"] == "update_alert"
        assert action["payload"]["subscribed_users"] == [7]

    def test_an_extra_subscriber_added_in_the_ui_is_not_dropped(self):
        ids = self._insight_ids()
        spec = php.alert_specs()[0]
        existing = {spec["name"]: {"id": 1, "insight_id": ids[spec["insight"]],
                                   "bounds": {"lower": 999}, "enabled": True,
                                   "subscribed_users": [9]}}
        action = php.plan_alerts([spec], existing, ids, [7])[0]
        assert action["payload"]["subscribed_users"] == [7, 9]

    def test_an_unknown_owner_never_reports_subscriber_drift(self):
        # whoami failed, so there is no one to add — reporting drift we can't fix would make every
        # dry run exit 2 forever.
        ids = self._insight_ids()
        spec = php.alert_specs()[0]
        existing = {spec["name"]: {"id": 1, "insight_id": ids[spec["insight"]],
                                   "bounds": spec["bounds"], "enabled": True,
                                   "subscribed_users": []}}
        assert php.plan_alerts([spec], existing, ids, [])[0]["action"] == "unchanged_alert"

    def test_threshold_drift_is_updated(self):
        ids = self._insight_ids()
        spec = php.alert_specs()[0]
        existing = {spec["name"]: {"id": 1, "insight_id": ids[spec["insight"]],
                                   "bounds": {"lower": 999}, "enabled": True}}
        action = php.plan_alerts([spec], existing, ids)[0]
        assert action["action"] == "update_alert" and action["alert_id"] == 1

    def test_a_disabled_alert_is_re_enabled(self):
        ids = self._insight_ids()
        spec = php.alert_specs()[0]
        existing = {spec["name"]: {"id": 1, "insight_id": ids[spec["insight"]],
                                   "bounds": spec["bounds"], "enabled": False}}
        assert php.plan_alerts([spec], existing, ids)[0]["action"] == "update_alert"


class TestPlanSubscriptions:
    def test_missing_dashboard_blocks(self):
        spec = php.subscription_spec("owner@example.com")
        assert php.plan_subscriptions([spec], [], {})[0]["action"] == "blocked_subscription"

    def test_creates_when_absent(self):
        spec = php.subscription_spec("owner@example.com")
        action = php.plan_subscriptions([spec], [], {php.DASH_GROWTH: 9})[0]
        assert action["action"] == "create_subscription"
        assert action["payload"]["dashboard"] == 9

    def test_existing_subscription_is_not_recreated_when_start_date_moved_on(self):
        # PostHog advances start_date with every send; diffing it would recreate the weekly email
        # on every run and spam the owner.
        spec = php.subscription_spec("Owner@Example.com")
        existing = [{"id": 3, "dashboard": 9, "target_value": "owner@example.com",
                     "target_type": "email", "frequency": "weekly",
                     "start_date": "1999-01-01T09:00:00Z"}]
        action = php.plan_subscriptions([spec], existing, {php.DASH_GROWTH: 9})[0]
        assert action["action"] == "unchanged_subscription" and action["subscription_id"] == 3

    def test_a_different_recipient_creates_a_second_subscription(self):
        spec = php.subscription_spec("new@example.com")
        existing = [{"id": 3, "dashboard": 9, "target_value": "old@example.com",
                     "frequency": "weekly"}]
        assert php.plan_subscriptions([spec], existing,
                                      {php.DASH_GROWTH: 9})[0]["action"] == "create_subscription"

    def test_a_deleted_subscription_is_replaced(self):
        spec = php.subscription_spec("owner@example.com")
        existing = [{"id": 3, "dashboard": 9, "target_value": "owner@example.com",
                     "frequency": "weekly", "deleted": True}]
        assert php.plan_subscriptions([spec], existing,
                                      {php.DASH_GROWTH: 9})[0]["action"] == "create_subscription"


class TestChannelDashboard:
    """The marketing-attribution dashboard (issue #658)."""

    def test_channel_tiles_never_mix_the_two_signup_events(self):
        # `signup_completed` (API) and `signup_completed_web` (browser) both fire for one signup.
        # A tile that read both would double every count, so each picks exactly one.
        for tile in php.channel_tiles():
            if tile["query"]["kind"] != "DataVisualizationNode":
                continue
            sql = _sql(tile)
            assert not ("'signup_completed'" in sql and "'signup_completed_web'" in sql), tile["name"]

    def test_activation_tile_reads_the_first_touch_person_property(self):
        # `activated` carries no UTMs of its own — only the person's first touch can attribute it.
        sql = _sql(_tile_by_name("Channels — Activations by first-touch channel (90d)"))
        assert "person.properties.initial_channel" in sql

    def test_brand_funnel_is_scoped_to_linkedin_traffic(self):
        source = _tile_by_name(
            "Channels — Brand post → site visit → signup funnel")["query"]["source"]
        steps = [s["event"] for s in source["series"]]
        assert steps == ["$pageview", "signup_started", "signup_completed"]
        assert source["series"][0]["properties"][0]["key"] == "utm_source"
        assert source["series"][0]["properties"][0]["value"] == ["linkedin"]
        # Only the first step is narrowed; narrowing the signup steps too would drop anyone who
        # navigated before converting.
        assert all("properties" not in s for s in source["series"][1:])

    def test_referral_tile_groups_on_the_ref_property(self):
        sql = _sql(_tile_by_name("Channels — Referral signups by referrer (90d)"))
        assert "properties.ref" in sql


class TestConversionGoals:
    def test_signup_goal_reads_the_browser_event_not_the_api_one(self):
        spec = next(s for s in php.conversion_goal_specs() if s["name"] == php.GOAL_SIGNUP)
        assert [step["event"] for step in spec["steps"]] == ["signup_completed_web"]

    def test_activation_goal_reads_the_activation_event(self):
        spec = next(s for s in php.conversion_goal_specs() if s["name"] == php.GOAL_ACTIVATION)
        assert [step["event"] for step in spec["steps"]] == ["activated"]

    def test_goals_only_reference_known_events(self):
        events = {step["event"] for s in php.conversion_goal_specs() for step in s["steps"]}
        assert events <= set(php.SOURCE_EVENTS)

    def test_goal_names_do_not_collide_with_insight_names(self):
        names = {s["name"] for s in php.conversion_goal_specs()}
        assert not (names & {tile["name"] for _, tile in _all_tiles()})

    def test_missing_goals_are_created(self):
        actions = php.plan_conversion_goals(php.conversion_goal_specs(), {})
        assert [a["action"] for a in actions] == ["create_goal", "create_goal"]

    def test_matching_goals_are_unchanged(self):
        existing = {s["name"]: {"id": i, "steps": [dict(step) for step in s["steps"]],
                                "description": s["description"]}
                    for i, s in enumerate(php.conversion_goal_specs(), start=1)}
        actions = php.plan_conversion_goals(php.conversion_goal_specs(), existing)
        assert all(a["action"] == "unchanged_goal" for a in actions)

    def test_posthogs_extra_step_fields_do_not_read_as_drift(self):
        # PostHog hydrates a step with its own id and a pile of nulls; comparing whole objects
        # would rewrite an action nobody touched on every single run.
        existing = {s["name"]: {"id": 1,
                                "steps": [{**step, "id": 99, "url": None, "selector": None}
                                          for step in s["steps"]],
                                "description": s["description"]}
                    for s in php.conversion_goal_specs()}
        actions = php.plan_conversion_goals(php.conversion_goal_specs(), existing)
        assert all(a["action"] == "unchanged_goal" for a in actions)

    def test_a_repointed_goal_is_updated(self):
        existing = {s["name"]: {"id": 1, "steps": [{"event": "wrong_event"}],
                                "description": s["description"]}
                    for s in php.conversion_goal_specs()}
        actions = php.plan_conversion_goals(php.conversion_goal_specs(), existing)
        assert all(a["action"] == "update_goal" for a in actions)

    def test_unreadable_actions_block_the_goals_without_claiming_they_are_missing(self):
        # A pre-#658 personal API key has no `action` scope. That must cost the two goals and
        # nothing else — an unreadable state is not an empty one, and reporting `create_goal` here
        # would promise an --apply that PostHog rejects.
        actions = php.plan_conversion_goals(php.conversion_goal_specs(), None)
        assert [a["action"] for a in actions] == ["blocked_goal", "blocked_goal"]
        client = MagicMock()
        lines = php.apply_actions(client, actions, dry_run=False)
        assert all(line.startswith("skipped ") for line in lines)
        client.create_action.assert_not_called()
        client.update_action.assert_not_called()

    def test_goal_payload_sends_only_the_event_per_step(self):
        spec = php.conversion_goal_specs()[0]
        payload = php._goal_payload(spec)
        assert payload["name"] == spec["name"]
        assert payload["steps"] == [{"event": "signup_completed_web"}]


class TestApplyActions:
    def _client(self):
        client = MagicMock()
        client.create_dashboard.return_value = 11
        client.create_insight.return_value = 22
        client.create_alert.return_value = 33
        client.create_subscription.return_value = 44
        client.create_action.return_value = 55
        client.list_dashboards.return_value = {php.DASH_HEALTH: 11, php.DASH_GROWTH: 12,
                                               php.DASH_CHANNELS: 13}
        return client

    def test_goal_actions_are_applied_and_dry_run_writes_nothing(self):
        client = self._client()
        actions = php.plan_conversion_goals(php.conversion_goal_specs(), {})
        assert all(line.startswith("[dry-run]")
                   for line in php.apply_actions(client, actions, dry_run=True))
        client.create_action.assert_not_called()
        php.apply_actions(client, actions, dry_run=False)
        assert client.create_action.call_count == 2

    def test_an_updated_goal_patches_rather_than_creates(self):
        client = self._client()
        existing = {s["name"]: {"id": 7, "steps": [{"event": "wrong_event"}], "description": ""}
                    for s in php.conversion_goal_specs()}
        actions = php.plan_conversion_goals(php.conversion_goal_specs(), existing)
        php.apply_actions(client, actions, dry_run=False)
        client.create_action.assert_not_called()
        assert client.update_action.call_count == 2

    def test_dry_run_writes_nothing(self):
        client = self._client()
        actions = php.plan_actions(php.build_specs(), {}, {})
        log = php.apply_actions(client, actions, dry_run=True)
        assert all(line.startswith("[dry-run]") for line in log)
        client.create_dashboard.assert_not_called()
        client.create_insight.assert_not_called()

    def test_apply_creates_dashboards_before_their_insights(self):
        client = self._client()
        actions = php.plan_actions(php.build_specs(), {}, {})
        php.apply_actions(client, actions, dry_run=False)
        assert client.create_dashboard.call_count == 3
        assert client.create_insight.call_count == len(_all_tiles())
        # Every insight lands on the id the create returned, not on a re-listed guess.
        assert all(call.args[1] == 11 for call in client.create_insight.call_args_list[:1])

    def test_apply_looks_up_pre_existing_dashboards_for_new_insights(self):
        client = self._client()
        specs = php.build_specs()[:1]
        actions = php.plan_actions(specs, {specs[0]["name"]: 11}, {})
        php.apply_actions(client, actions, dry_run=False)
        client.list_dashboards.assert_called()
        assert all(call.args[1] == 11 for call in client.create_insight.call_args_list)

    def test_apply_writes_alerts_and_subscriptions(self):
        client = self._client()
        ids = {spec["insight"]: 100 for spec in php.alert_specs()}
        actions = php.plan_alerts(php.alert_specs(), {}, ids, [7])
        actions += php.plan_subscriptions([php.subscription_spec("owner@example.com")], [],
                                          {php.DASH_GROWTH: 12})
        log = php.apply_actions(client, actions, dry_run=False)
        assert client.create_alert.call_count == len(php.alert_specs())
        assert client.create_subscription.call_count == 1
        assert any("created subscription" in line for line in log)

    def test_blocked_items_are_reported_not_written(self):
        client = self._client()
        log = php.apply_actions(client, php.plan_alerts(php.alert_specs(), {}, {}), dry_run=False)
        client.create_alert.assert_not_called()
        assert all(line.startswith("skipped") for line in log)

    def test_creates_the_distinct_id_variable(self):
        client = self._client()
        client.create_insight_variable.return_value = {"id": "v1", "code_name": "distinct_id"}
        action = php.plan_variable({})
        log = php.apply_actions(client, [action], dry_run=False)
        client.create_insight_variable.assert_called_once_with(action["payload"])
        assert any("created insight variable" in line for line in log)

    def test_variable_dry_run_writes_nothing(self):
        client = self._client()
        php.apply_actions(client, [php.plan_variable({})], dry_run=True)
        client.create_insight_variable.assert_not_called()

    def test_creates_and_updates_endpoints(self):
        client = self._client()
        specs = php.endpoint_specs()
        actions = php.plan_endpoints(specs, {}, "v1", "distinct_id")
        php.apply_actions(client, actions, dry_run=False)
        assert client.create_endpoint.call_count == 3
        client.update_endpoint.assert_not_called()

        client.reset_mock()
        stale = {specs[0]["name"]: {"query": {"kind": "edited-away"},
                                    "description": specs[0]["description"],
                                    "data_freshness_seconds": php.ENDPOINT_DATA_FRESHNESS_SECONDS,
                                    "is_active": True}}
        update_actions = php.plan_endpoints(specs[:1], stale, "v1", "distinct_id")
        php.apply_actions(client, update_actions, dry_run=False)
        client.update_endpoint.assert_called_once_with(specs[0]["name"], update_actions[0]["payload"])
        client.create_endpoint.assert_not_called()

    def test_endpoint_blocked_items_are_reported_not_written(self):
        client = self._client()
        log = php.apply_actions(client, php.plan_endpoints(php.endpoint_specs(), {}, None, None),
                                dry_run=False)
        client.create_endpoint.assert_not_called()
        assert all(line.startswith("skipped") for line in log)

    def test_summarize_counts_every_action_kind(self):
        summary = php.summarize([{"action": "create_insight"}, {"action": "create_insight"},
                                 {"action": "unchanged"}])
        assert summary == "create_insight=2, unchanged=1"

    def test_summarize_empty(self):
        assert php.summarize([]) == "nothing to do"

    def test_dashboard_url(self):
        assert php.dashboard_url("https://us.posthog.com/", "1", 9) == \
            "https://us.posthog.com/project/1/dashboard/9"


class TestClientNormalization:
    def test_alert_insight_id_accepts_both_api_shapes(self):
        assert php._insight_id_of(7) == 7
        assert php._insight_id_of({"id": 7, "name": "x"}) == 7
        assert php._insight_id_of(None) is None

    def test_subscriber_id_accepts_both_api_shapes(self):
        # PostHog serializes subscribed_users as nested user objects on read, bare ids on write.
        assert php._user_id_of(7) == 7
        assert php._user_id_of({"id": 7, "email": "owner@example.com"}) == 7
        assert php._user_id_of(None) is None


class TestEndpointSpecs:
    def test_three_endpoints_scoped_to_one_user(self):
        specs = php.endpoint_specs()
        assert {s["name"] for s in specs} == {
            php.ENDPOINT_POSTS_ENGAGEMENT, php.ENDPOINT_COMMENT_ACTIVITY,
            php.ENDPOINT_LLM_COST_BY_FEATURE}
        for spec in specs:
            assert "distinct_id = {variables.distinct_id}" in spec["sql"]
            assert spec["description"]

    def test_names_are_valid_endpoint_slugs(self):
        # PostHog's ENDPOINT_NAME_REGEX: starts with a letter, only alnum/hyphen/underscore.
        for spec in php.endpoint_specs():
            assert re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*", spec["name"])

    def test_llm_cost_endpoint_reads_llm_call_not_ai_generation(self):
        # Money questions use llm_call (LEM's own estimate, keyed by feature) — never
        # $ai_generation, per docs/llm-analytics.md.
        spec = next(s for s in php.endpoint_specs() if s["name"] == php.ENDPOINT_LLM_COST_BY_FEATURE)
        assert "llm_call" in spec["sql"] and "$ai_generation" not in spec["sql"]

    def test_no_name_collision_with_insight_tiles(self):
        tile_names = {tile["name"] for _, tile in _all_tiles()}
        endpoint_names = {s["name"] for s in php.endpoint_specs()}
        assert not (tile_names & endpoint_names)


class TestEndpointQuery:
    def test_binds_the_live_variable_id_and_code_name(self):
        spec = php.endpoint_specs()[0]
        query = php.endpoint_query(spec, "var-uuid-1", "distinct_id")
        assert query["kind"] == "HogQLQuery"
        assert query["query"] == spec["sql"].strip()
        assert query["variables"] == {
            "var-uuid-1": {"variableId": "var-uuid-1", "code_name": "distinct_id",
                          "value": None, "isNull": True}}

    def test_variable_id_is_stringified(self):
        # PostHog variable ids are UUIDs; a caller that passes a non-str (e.g. from a raw API
        # response) must not produce a query with a mismatched key type.
        query = php.endpoint_query(php.endpoint_specs()[0], 42, "distinct_id")
        assert list(query["variables"].keys()) == ["42"]


class TestPlanVariable:
    def test_missing_variable_is_created(self):
        action = php.plan_variable({})
        assert action["action"] == "create_variable"
        assert action["payload"] == {"name": php.DISTINCT_ID_VARIABLE_NAME, "type": "String",
                                     "default_value": None}

    def test_existing_variable_is_unchanged(self):
        existing = {php.DISTINCT_ID_VARIABLE_NAME: {"id": "v1", "code_name": "distinct_id"}}
        action = php.plan_variable(existing)
        assert action == {"action": "unchanged_variable", "id": "v1", "code_name": "distinct_id"}


class TestPlanEndpoints:
    def test_blocked_when_variable_does_not_exist(self):
        actions = php.plan_endpoints(php.endpoint_specs(), {}, None, None)
        assert {a["action"] for a in actions} == {"blocked_endpoint"}
        assert php.pending(actions) == actions

    def test_empty_project_creates_all_three(self):
        actions = php.plan_endpoints(php.endpoint_specs(), {}, "v1", "distinct_id")
        assert {a["action"] for a in actions} == {"create_endpoint"}
        assert len(actions) == 3

    def test_matching_state_is_unchanged(self):
        specs = php.endpoint_specs()
        existing = {s["name"]: {"query": php.endpoint_query(s, "v1", "distinct_id"),
                                "description": s["description"],
                                "data_freshness_seconds": php.ENDPOINT_DATA_FRESHNESS_SECONDS,
                                "is_active": True}
                    for s in specs}
        actions = php.plan_endpoints(specs, existing, "v1", "distinct_id")
        assert {a["action"] for a in actions} == {"unchanged_endpoint"}
        assert php.pending(actions) == []

    def test_drifted_query_is_updated(self):
        specs = php.endpoint_specs()[:1]
        existing = {specs[0]["name"]: {"query": {"kind": "edited-away"},
                                       "description": specs[0]["description"],
                                       "data_freshness_seconds": php.ENDPOINT_DATA_FRESHNESS_SECONDS,
                                       "is_active": True}}
        action = php.plan_endpoints(specs, existing, "v1", "distinct_id")[0]
        assert action["action"] == "update_endpoint"

    def test_inactive_endpoint_is_reactivated(self):
        specs = php.endpoint_specs()[:1]
        existing = {specs[0]["name"]: {"query": php.endpoint_query(specs[0], "v1", "distinct_id"),
                                       "description": specs[0]["description"],
                                       "data_freshness_seconds": php.ENDPOINT_DATA_FRESHNESS_SECONDS,
                                       "is_active": False}}
        action = php.plan_endpoints(specs, existing, "v1", "distinct_id")[0]
        assert action["action"] == "update_endpoint"

    def test_a_different_variable_id_forces_an_update(self):
        # The variable was recreated (a fresh id) — every endpoint's stored `variables` map now
        # points at a dangling id and must be repointed.
        specs = php.endpoint_specs()[:1]
        existing = {specs[0]["name"]: {"query": php.endpoint_query(specs[0], "stale-id", "distinct_id"),
                                       "description": specs[0]["description"],
                                       "data_freshness_seconds": php.ENDPOINT_DATA_FRESHNESS_SECONDS,
                                       "is_active": True}}
        action = php.plan_endpoints(specs, existing, "fresh-id", "distinct_id")[0]
        assert action["action"] == "update_endpoint"


class TestMain:
    def _client(self, **overrides):
        client = MagicMock()
        client.list_dashboards.return_value = {}
        client.list_insights.return_value = {}
        client.list_alerts.return_value = {}
        client.list_subscriptions.return_value = []
        client.list_actions.return_value = {}
        client.list_insight_variables.return_value = {}
        client.list_endpoints.return_value = {}
        client.whoami.return_value = {"id": 7, "email": "owner@example.com"}
        client.create_dashboard.return_value = 11
        client.create_insight.return_value = 22
        client.create_action.return_value = 55
        client.create_insight_variable.return_value = {"id": "var-1", "code_name": "distinct_id"}
        for key, value in overrides.items():
            getattr(client, key).return_value = value
        return client

    def test_print_sql_needs_no_network(self, capsys, monkeypatch):
        monkeypatch.delenv("POSTHOG_PERSONAL_API_KEY", raising=False)
        assert php.main(["--print-sql"]) == 0
        out = capsys.readouterr().out
        assert php.DASH_HEALTH in out and "FunnelsQuery" in out

    def test_simulate_reports_a_synthetic_breach(self, capsys):
        name = php.alert_specs()[0]["name"]
        assert php.main([f"--simulate={name}=0"]) == 0
        assert "BREACH" in capsys.readouterr().out

    def test_simulate_reports_a_healthy_value(self, capsys):
        spec = next(s for s in php.alert_specs() if "upper" in s["bounds"])
        assert php.main([f"--simulate={spec['name']}=0"]) == 0
        assert "ok" in capsys.readouterr().out

    def test_simulate_unknown_alert_errors(self, capsys):
        assert php.main(["--simulate=nope=1"]) == 1
        assert "Unknown alert" in capsys.readouterr().err

    def test_missing_api_key_is_an_error(self, monkeypatch, capsys):
        monkeypatch.delenv("POSTHOG_PERSONAL_API_KEY", raising=False)
        assert php.main([]) == 1
        assert "POSTHOG_PERSONAL_API_KEY" in capsys.readouterr().err

    def test_operator_key_alone_reaches_the_client(self, monkeypatch, capsys):
        # issue #1453 follow-up: this hand-run script reads POSTHOG_OPERATOR_API_KEY, not the
        # shared POSTHOG_PERSONAL_API_KEY directly.
        monkeypatch.delenv("POSTHOG_PERSONAL_API_KEY", raising=False)
        monkeypatch.setenv("POSTHOG_OPERATOR_API_KEY", "phx_operator")
        monkeypatch.setenv("POSTHOG_REPORT_EMAIL", "owner@example.com")
        client = self._client()
        monkeypatch.setattr(php, "PostHogClient", lambda *a, **k: client)
        assert php.main(["--dry-run"]) == 2
        assert "[dry-run] create dashboard" in capsys.readouterr().out

    def test_dry_run_on_an_empty_project_reports_pending(self, monkeypatch, capsys):
        monkeypatch.setenv("POSTHOG_PERSONAL_API_KEY", "phx_test")
        monkeypatch.setenv("POSTHOG_REPORT_EMAIL", "owner@example.com")
        client = self._client()
        monkeypatch.setattr(php, "PostHogClient", lambda *a, **k: client)
        assert php.main(["--dry-run"]) == 2
        out = capsys.readouterr().out
        assert "[dry-run] create dashboard" in out
        client.create_dashboard.assert_not_called()

    def test_apply_provisions_dashboards_alerts_and_the_report(self, monkeypatch, capsys):
        monkeypatch.setenv("POSTHOG_PERSONAL_API_KEY", "phx_test")
        monkeypatch.delenv("POSTHOG_REPORT_EMAIL", raising=False)
        client = self._client()
        # After the insight pass the ids exist, so alerts and the subscription can be planned.
        created = {tile["name"]: {"id": 500 + i, "query": tile["query"],
                                  "description": tile["description"], "dashboards": [11]}
                   for i, (_, tile) in enumerate(_all_tiles())}
        client.list_insights.side_effect = [{}, created]
        client.list_dashboards.side_effect = [{}, {php.DASH_HEALTH: 11, php.DASH_GROWTH: 12},
                                              {php.DASH_HEALTH: 11, php.DASH_GROWTH: 12}]
        monkeypatch.setattr(php, "PostHogClient", lambda *a, **k: client)
        assert php.main(["--apply"]) == 0
        assert client.create_alert.call_count == len(php.alert_specs())
        client.create_subscription.assert_called_once()
        # Recipient falls back to the API key owner when POSTHOG_REPORT_EMAIL is unset.
        assert client.create_subscription.call_args.args[0]["target_value"] == "owner@example.com"

    def test_unreadable_state_is_an_error(self, monkeypatch, capsys):
        monkeypatch.setenv("POSTHOG_PERSONAL_API_KEY", "phx_test")
        client = self._client()
        client.list_dashboards.side_effect = RuntimeError("boom")
        monkeypatch.setattr(php, "PostHogClient", lambda *a, **k: client)
        assert php.main(["--dry-run"]) == 1
        assert "Failed to read PostHog state" in capsys.readouterr().err

    def test_whoami_failure_still_provisions_dashboards(self, monkeypatch, capsys):
        monkeypatch.setenv("POSTHOG_PERSONAL_API_KEY", "phx_test")
        monkeypatch.delenv("POSTHOG_REPORT_EMAIL", raising=False)
        client = self._client()
        client.whoami.side_effect = RuntimeError("no user:read scope")
        monkeypatch.setattr(php, "PostHogClient", lambda *a, **k: client)
        assert php.main(["--dry-run"]) == 2
        err = capsys.readouterr().err
        assert "alerts will have no subscriber" in err
        assert "weekly report not provisioned" in err

    def test_endpoints_are_blocked_until_the_variable_exists(self, monkeypatch, capsys):
        # A dry run never claims it can create an endpoint before the variable behind it exists.
        monkeypatch.setenv("POSTHOG_PERSONAL_API_KEY", "phx_test")
        monkeypatch.setenv("POSTHOG_REPORT_EMAIL", "owner@example.com")
        client = self._client()
        monkeypatch.setattr(php, "PostHogClient", lambda *a, **k: client)
        assert php.main(["--dry-run"]) == 2
        out = capsys.readouterr().out
        assert "[dry-run] create insight variable 'distinct_id'" in out
        assert "skipped 'lem-posts-engagement-weekly'" in out
        client.create_endpoint.assert_not_called()

    def test_apply_provisions_endpoints_once_the_variable_exists(self, monkeypatch, capsys):
        monkeypatch.setenv("POSTHOG_PERSONAL_API_KEY", "phx_test")
        monkeypatch.setenv("POSTHOG_REPORT_EMAIL", "owner@example.com")
        client = self._client()
        # Freshly created, so the id/code_name only exist on the RE-read after create — the same
        # create-then-reread shape the dashboard/insight pass uses.
        client.list_insight_variables.side_effect = [
            {}, {php.DISTINCT_ID_VARIABLE_NAME: {"id": "v1", "code_name": "distinct_id"}}]
        monkeypatch.setattr(php, "PostHogClient", lambda *a, **k: client)
        assert php.main(["--apply"]) == 0
        client.create_insight_variable.assert_called_once()
        assert client.create_endpoint.call_count == 3
        for call in client.create_endpoint.call_args_list:
            assert call.args[0]["query"]["variables"] == {
                "v1": {"variableId": "v1", "code_name": "distinct_id", "value": None, "isNull": True}}
