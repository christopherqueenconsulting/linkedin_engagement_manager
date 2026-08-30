"""Unit tests for scripts/posthog_dashboards.py — the tile specs and the create/update planner."""

import importlib.util
import pathlib
import re

import pytest

pytestmark = pytest.mark.unit

# The tool lives under scripts/ (not an importable package) — load it by path.
_PATH = pathlib.Path(__file__).resolve().parents[2] / "scripts" / "posthog_dashboards.py"
_spec = importlib.util.spec_from_file_location("posthog_dashboards", _PATH)
phd = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(phd)


def _all_tiles():
    return [(spec["name"], tile) for spec in phd.build_specs() for tile in spec["tiles"]]


def _sql(tile):
    return tile["query"]["source"]["query"]


class TestSpecs:
    def test_four_dashboards_matching_the_plan(self):
        names = [spec["name"] for spec in phd.build_specs()]
        assert names == [phd.DASH_COST, phd.DASH_MARGIN, phd.DASH_LIFT, phd.DASH_SCORECARD]
        assert all(spec["tiles"] for spec in phd.build_specs())

    def test_tile_names_are_globally_unique(self):
        # plan_actions() keys existing insights by name, so a duplicate name would make two tiles
        # fight over one insight forever.
        names = [tile["name"] for _, tile in _all_tiles()]
        assert len(names) == len(set(names))

    def test_every_tile_is_a_hogql_visualization(self):
        for dashboard, tile in _all_tiles():
            query = tile["query"]
            assert query["kind"] == "DataVisualizationNode", f"{dashboard}/{tile['name']}"
            assert query["source"]["kind"] == "HogQLQuery"
            assert _sql(tile).startswith(("SELECT", "WITH"))
            assert tile["description"]

    def test_descriptions_fit_the_posthog_limit(self):
        for _, tile in _all_tiles():
            assert len(tile["description"]) <= 400
        for spec in phd.build_specs():
            assert len(spec["description"]) <= 400

    def test_tiles_only_read_known_lem_events(self):
        for dashboard, tile in _all_tiles():
            referenced = set(re.findall(r"event (?:=|IN) \(?'([a-z_]+)'", _sql(tile)))
            referenced |= set(re.findall(r"', '([a-z_]+)'\)", _sql(tile)))
            unknown = referenced - set(phd.SOURCE_EVENTS)
            assert not unknown, f"{dashboard}/{tile['name']} reads unknown events {unknown}"

    def test_chart_axes_reference_columns_the_query_selects(self):
        for dashboard, tile in _all_tiles():
            settings = tile["query"].get("chartSettings")
            if not settings:
                continue
            aliases = set(re.findall(r"AS (\w+)", _sql(tile)))
            columns = [settings["xAxis"]["column"]] + [y["column"] for y in settings["yAxis"]]
            missing = [c for c in columns if c not in aliases]
            assert not missing, f"{dashboard}/{tile['name']} plots missing columns {missing}"

    def test_margin_target_is_drawn_as_a_goal_line(self):
        tiles = {tile["name"]: tile for tile in phd.margin_by_cohort_tiles()}
        goals = tiles["Gross margin % vs target"]["query"]["chartSettings"]["goalLines"]
        assert goals[0]["value"] == phd.TARGET_GROSS_MARGIN_PCT

    def test_lift_tiles_filter_before_joining(self):
        # An unfiltered `JOIN events` here runs the query out of memory in PostHog.
        for tile in phd.engagement_lift_tiles():
            sql = _sql(tile)
            if "JOIN" in sql:
                assert phd.BASELINE_CTES in sql
                assert "JOIN events" not in sql

    def test_scorecard_leads_with_the_north_star(self):
        assert phd.scorecard_tiles()[0]["name"].startswith("★ System gross margin")


class TestPlanActions:
    def test_empty_project_creates_everything(self):
        specs = phd.build_specs()
        actions = phd.plan_actions(specs, {}, {})
        created_dashboards = [a for a in actions if a["action"] == "create_dashboard"]
        created_insights = [a for a in actions if a["action"] == "create_insight"]
        assert len(created_dashboards) == len(specs)
        assert len(created_insights) == sum(len(s["tiles"]) for s in specs)
        assert not [a for a in actions if a["action"] in ("unchanged", "update_insight")]

    def test_dashboard_create_precedes_its_insights(self):
        actions = phd.plan_actions(phd.build_specs(), {}, {})
        first = next(i for i, a in enumerate(actions) if a["action"] == "create_dashboard")
        first_insight = next(i for i, a in enumerate(actions) if a["action"] == "create_insight")
        assert first < first_insight

    def test_matching_insight_attached_to_the_dashboard_is_unchanged(self):
        spec = phd.build_specs()[0]
        tile = spec["tiles"][0]
        actions = phd.plan_actions(
            [{"name": spec["name"], "description": spec["description"], "tiles": [tile]}],
            {spec["name"]: 7},
            {tile["name"]: {"id": 42, "query": tile["query"], "dashboards": [7]}},
        )
        assert [a["action"] for a in actions] == ["unchanged"]
        assert actions[0]["insight_id"] == 42

    def test_drifted_query_is_updated(self):
        spec = phd.build_specs()[0]
        tile = spec["tiles"][0]
        actions = phd.plan_actions(
            [{"name": spec["name"], "description": spec["description"], "tiles": [tile]}],
            {spec["name"]: 7},
            {tile["name"]: {"id": 42, "query": {"kind": "edited-in-the-ui"}, "dashboards": [7]}},
        )
        assert [a["action"] for a in actions] == ["update_insight"]
        assert actions[0]["tile"] == tile

    def test_drifted_description_is_updated(self):
        spec = phd.build_specs()[0]
        tile = spec["tiles"][0]
        actions = phd.plan_actions(
            [{"name": spec["name"], "description": spec["description"], "tiles": [tile]}],
            {spec["name"]: 7},
            {tile["name"]: {"id": 42, "query": tile["query"], "description": "edited in the UI",
                            "dashboards": [7]}},
        )
        assert [a["action"] for a in actions] == ["update_insight"]

    def test_matching_description_stays_unchanged(self):
        spec = phd.build_specs()[0]
        tile = spec["tiles"][0]
        actions = phd.plan_actions(
            [{"name": spec["name"], "description": spec["description"], "tiles": [tile]}],
            {spec["name"]: 7},
            {tile["name"]: {"id": 42, "query": tile["query"],
                            "description": tile["description"], "dashboards": [7]}},
        )
        assert [a["action"] for a in actions] == ["unchanged"]

    def test_detached_insight_is_reattached(self):
        spec = phd.build_specs()[0]
        tile = spec["tiles"][0]
        actions = phd.plan_actions(
            [{"name": spec["name"], "description": spec["description"], "tiles": [tile]}],
            {spec["name"]: 7},
            {tile["name"]: {"id": 42, "query": tile["query"], "dashboards": []}},
        )
        assert [a["action"] for a in actions] == ["update_insight"]

    def test_pending_and_summarize(self):
        actions = [{"action": "unchanged", "dashboard": "d"},
                   {"action": "create_insight", "dashboard": "d"}]
        assert phd.pending(actions) == [actions[1]]
        assert phd.summarize(actions) == "create_insight=1, unchanged=1"
        assert phd.summarize([]) == "nothing to do"

    def test_dashboard_url_is_project_scoped(self):
        assert phd.dashboard_url("https://us.posthog.com/", "475262", 99) == \
            "https://us.posthog.com/project/475262/dashboard/99"


class _FakeClient:
    def __init__(self, dashboards=None):
        self.dashboards = dict(dashboards or {})
        self.created_dashboards, self.created_insights, self.updated_insights = [], [], []
        self._next_id = 100

    def create_dashboard(self, name, description):
        self._next_id += 1
        self.dashboards[name] = self._next_id
        self.created_dashboards.append((name, description))
        return self._next_id

    def create_insight(self, tile, dashboard_id):
        self._next_id += 1
        self.created_insights.append((tile["name"], dashboard_id))
        return self._next_id

    def update_insight(self, insight_id, tile, dashboard_id):
        self.updated_insights.append((insight_id, tile["name"], dashboard_id))

    def list_dashboards(self):
        return dict(self.dashboards)


class TestApplyActions:
    def test_dry_run_writes_nothing(self):
        client = _FakeClient()
        log = phd.apply_actions(client, phd.plan_actions(phd.build_specs(), {}, {}), dry_run=True)
        assert client.created_dashboards == [] and client.created_insights == []
        assert all(line.startswith("[dry-run]") for line in log)

    def test_apply_creates_dashboard_then_attaches_its_insights(self):
        client = _FakeClient()
        specs = phd.build_specs()[:1]
        phd.apply_actions(client, phd.plan_actions(specs, {}, {}), dry_run=False)
        dashboard_id = client.dashboards[specs[0]["name"]]
        assert [name for name, _ in client.created_dashboards] == [specs[0]["name"]]
        assert [d for _, d in client.created_insights] == [dashboard_id] * len(specs[0]["tiles"])

    def test_apply_resolves_an_existing_dashboard_id(self):
        spec = phd.build_specs()[0]
        tile = spec["tiles"][0]
        client = _FakeClient({spec["name"]: 7})
        actions = phd.plan_actions(
            [{"name": spec["name"], "description": spec["description"], "tiles": [tile]}],
            {spec["name"]: 7},
            {tile["name"]: {"id": 42, "query": {"kind": "stale"}, "dashboards": [7]}},
        )
        phd.apply_actions(client, actions, dry_run=False)
        assert client.updated_insights == [(42, tile["name"], 7)]
        assert client.created_dashboards == []


class TestMain:
    def test_print_sql_needs_no_credentials(self, capsys, monkeypatch):
        monkeypatch.delenv("POSTHOG_PERSONAL_API_KEY", raising=False)
        assert phd.main(["--print-sql"]) == 0
        out = capsys.readouterr().out
        assert phd.DASH_COST in out and "SELECT" in out

    def test_missing_api_key_fails_fast(self, monkeypatch):
        monkeypatch.setenv("POSTHOG_PERSONAL_API_KEY", "")
        assert phd.main([]) == 1

    def test_operator_key_alone_reaches_the_client(self, monkeypatch, capsys):
        # issue #1453 follow-up: the hand-run scripts read POSTHOG_OPERATOR_API_KEY, not the shared
        # POSTHOG_PERSONAL_API_KEY directly — this proves it is what reaches the client.
        monkeypatch.delenv("POSTHOG_PERSONAL_API_KEY", raising=False)
        monkeypatch.setenv("POSTHOG_OPERATOR_API_KEY", "phx_operator")
        captured = {}

        class _Stub:
            def __init__(self, api_key, project_id, app_host):
                captured["api_key"] = api_key

            def list_dashboards(self):
                raise RuntimeError("stop after the key gate")

            def list_insights(self):
                return {}

        monkeypatch.setattr(phd, "PostHogClient", _Stub)
        assert phd.main([]) == 1
        assert captured["api_key"] == "phx_operator"
        assert "Failed to read PostHog state" in capsys.readouterr().err

    def test_apply_and_dry_run_are_mutually_exclusive(self):
        # Otherwise `--apply --dry-run` would still write, the opposite of what the user asked for.
        with pytest.raises(SystemExit):
            phd.main(["--apply", "--dry-run"])
