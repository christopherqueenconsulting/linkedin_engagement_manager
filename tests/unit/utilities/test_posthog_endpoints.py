"""Unit tests for utilities/posthog_endpoints.py — the runtime client behind the in-SPA "your
stats" panel (issue #654)."""

from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.unit

_MOD = "cqc_lem.utilities.posthog_endpoints"


def _resp(payload, status=200):
    r = MagicMock()
    r.json.return_value = payload
    r.raise_for_status.side_effect = None if status == 200 else Exception(f"http {status}")
    return r


class TestRunEndpoint:
    def test_no_api_key_returns_none_without_a_network_call(self, monkeypatch):
        from cqc_lem.utilities.posthog_endpoints import run_endpoint
        monkeypatch.delenv("POSTHOG_PERSONAL_API_KEY", raising=False)
        with patch(f"{_MOD}.requests.post") as post:
            assert run_endpoint("lem-posts-engagement-weekly", 7) is None
        post.assert_not_called()

    def test_scopes_the_call_to_the_caller_s_own_distinct_id(self, monkeypatch):
        from cqc_lem.utilities.posthog_endpoints import run_endpoint
        monkeypatch.setenv("POSTHOG_PERSONAL_API_KEY", "phx_test")
        with patch(f"{_MOD}.requests.post", return_value=_resp({"results": [], "columns": []})) as post:
            run_endpoint("lem-posts-engagement-weekly", 7)
        url, kwargs = post.call_args.args[0], post.call_args.kwargs
        assert url.endswith("/endpoints/lem-posts-engagement-weekly/run/")
        assert kwargs["json"] == {"variables": {"distinct_id": "7"}}
        assert kwargs["headers"]["Authorization"] == "Bearer phx_test"

    def test_default_project_and_host_when_unset(self, monkeypatch):
        from cqc_lem.utilities.posthog_endpoints import run_endpoint
        monkeypatch.setenv("POSTHOG_PERSONAL_API_KEY", "phx_test")
        monkeypatch.delenv("POSTHOG_PROJECT_ID", raising=False)
        monkeypatch.delenv("POSTHOG_APP_HOST", raising=False)
        with patch(f"{_MOD}.requests.post", return_value=_resp({})) as post:
            run_endpoint("lem-comment-activity-weekly", 3)
        assert post.call_args.args[0] == \
            "https://us.posthog.com/api/projects/475262/endpoints/lem-comment-activity-weekly/run/"

    def test_http_error_returns_none(self, monkeypatch):
        from cqc_lem.utilities.posthog_endpoints import run_endpoint
        monkeypatch.setenv("POSTHOG_PERSONAL_API_KEY", "phx_test")
        with patch(f"{_MOD}.requests.post", return_value=_resp({}, status=500)):
            assert run_endpoint("lem-llm-cost-by-feature", 7) is None

    def test_network_exception_returns_none(self, monkeypatch):
        from cqc_lem.utilities.posthog_endpoints import run_endpoint
        monkeypatch.setenv("POSTHOG_PERSONAL_API_KEY", "phx_test")
        with patch(f"{_MOD}.requests.post", side_effect=ConnectionError("down")):
            assert run_endpoint("lem-llm-cost-by-feature", 7) is None


class TestGetUserStatsPanel:
    def test_no_key_reports_every_panel_unavailable(self, monkeypatch):
        from cqc_lem.utilities.posthog_endpoints import get_user_stats_panel
        monkeypatch.delenv("POSTHOG_PERSONAL_API_KEY", raising=False)
        panel = get_user_stats_panel(7)
        assert set(panel) == {"posts_engagement", "comment_activity", "llm_cost_by_feature"}
        assert all(p == {"available": False, "rows": []} for p in panel.values())

    def test_reshapes_columnar_results_into_row_dicts(self, monkeypatch):
        from cqc_lem.utilities.posthog_endpoints import get_user_stats_panel
        monkeypatch.setenv("POSTHOG_PERSONAL_API_KEY", "phx_test")
        payload = {"columns": ["feature", "spend_usd", "calls"],
                   "results": [["content", 1.5, 10], ["comment", 0.25, 4]]}
        with patch(f"{_MOD}.requests.post", return_value=_resp(payload)):
            panel = get_user_stats_panel(7)
        for key in panel:
            assert panel[key]["available"] is True
            assert panel[key]["rows"] == [
                {"feature": "content", "spend_usd": 1.5, "calls": 10},
                {"feature": "comment", "spend_usd": 0.25, "calls": 4}]

    def test_a_partial_outage_still_reports_the_endpoints_that_answered(self, monkeypatch):
        from cqc_lem.utilities.posthog_endpoints import ENDPOINT_LLM_COST_BY_FEATURE
        import cqc_lem.utilities.posthog_endpoints as mod
        monkeypatch.setenv("POSTHOG_PERSONAL_API_KEY", "phx_test")

        def fake_run(name, user_id):
            if name == ENDPOINT_LLM_COST_BY_FEATURE:
                return None
            return {"columns": ["week"], "results": [["2026-07-20"]]}

        with patch.object(mod, "run_endpoint", side_effect=fake_run):
            panel = mod.get_user_stats_panel(7)
        assert panel["llm_cost_by_feature"] == {"available": False, "rows": []}
        assert panel["posts_engagement"]["available"] is True
        assert panel["comment_activity"]["available"] is True
