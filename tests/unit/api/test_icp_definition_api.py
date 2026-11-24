"""Unit tests for GET/PUT /api/user/icp-definition and the roster/leads pre-fill (issue #2655)."""

from unittest.mock import patch

import pytest

from cqc_lem.utilities.icp_prefill import draft_icp_definition, seed_profile_urls

pytestmark = pytest.mark.unit

_USER = 7
_SEGMENT = {"id": "s1", "name": "Founders", "role_titles": ["founder"]}
_R = "cqc_lem.api.routers.user."


def _get(api_client, **patches):
    prefs = patches.pop("prefs", {})
    with patch("cqc_lem.api.main.get_session_user_id", return_value=_USER), \
         patch(_R + "get_engagement_preferences", return_value=prefs), \
         patch(_R + "get_engagement_targets", return_value=patches.get("roster", [])), \
         patch(_R + "get_leads", return_value={"leads": patches.get("leads", [])}), \
         patch(_R + "get_profile_facts", return_value=patches.get("facts", {})):
        return api_client.get("/api/user/icp-definition?session_token=t")


class TestGet:
    def test_401(self, api_client):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=None):
            assert api_client.get("/api/user/icp-definition?session_token=x").status_code == 401

    def test_saved_definition_has_no_draft(self, api_client):
        resp = _get(api_client, prefs={"icp_builder_enabled": True,
                                       "icp_definition": {"segments": [_SEGMENT]}})
        detail = resp.json()["detail"]
        assert detail["icp_builder_enabled"] is True
        assert detail["icp_definition"]["segments"][0]["id"] == "s1"
        assert detail["draft"] is None

    def test_draft_built_when_nothing_saved(self, api_client):
        url = "https://www.linkedin.com/in/jane"
        resp = _get(api_client, roster=[{"category": "icp", "profile_url": url}],
                    facts={url: {"job_title": "CTO", "industry": "Software"}})
        detail = resp.json()["detail"]
        assert detail["icp_definition"] is None
        assert detail["draft"]["segments"][0]["role_titles"] == ["CTO"]
        assert detail["draft_sample_size"] == 1

    def test_no_seed_people_means_no_draft(self, api_client):
        detail = _get(api_client).json()["detail"]
        assert detail["draft"] is None and detail["draft_sample_size"] == 0

    def test_draft_failure_never_fails_the_editor(self, api_client):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=_USER), \
             patch(_R + "get_engagement_preferences", return_value={}), \
             patch(_R + "get_engagement_targets", side_effect=RuntimeError("db")):
            resp = api_client.get("/api/user/icp-definition?session_token=t")
        assert resp.status_code == 200 and resp.json()["detail"]["draft"] is None


class TestPut:
    def _put(self, api_client, body, ok=True):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=_USER), \
             patch(_R + "update_engagement_preferences", return_value=ok) as upd, \
             patch(_R + "get_engagement_preferences", return_value={}), \
             patch(_R + "get_engagement_targets", return_value=[]), \
             patch(_R + "get_leads", return_value={"leads": []}):
            resp = api_client.put("/api/user/icp-definition",
                                  json={"session_token": "t", **body})
        return resp, upd

    def test_saves_valid_definition_and_flag(self, api_client):
        resp, upd = self._put(api_client, {"icp_builder_enabled": True,
                                           "icp_definition": {"segments": [_SEGMENT]}})
        assert resp.status_code == 200
        saved = upd.call_args[0][1]
        assert saved["icp_builder_enabled"] is True
        assert saved["icp_definition"]["segments"][0]["id"] == "s1"

    def test_omitted_fields_are_not_written(self, api_client):
        _, upd = self._put(api_client, {"icp_builder_enabled": False})
        assert upd.call_args[0][1] == {"icp_builder_enabled": False}

    def test_empty_object_clears(self, api_client):
        _, upd = self._put(api_client, {"icp_definition": {}})
        assert upd.call_args[0][1] == {"icp_definition": {}}

    @pytest.mark.parametrize("definition", [
        {"segments": []},
        {"segments": [{"id": "", "name": "x"}]},
        {"segments": [_SEGMENT, _SEGMENT]},
        {"segments": [{"id": "a", "company_size_min": -1}]},
        {"exclusions": {}},
    ])
    def test_invalid_definition_is_422(self, api_client, definition):
        resp, upd = self._put(api_client, {"icp_definition": definition})
        assert resp.status_code == 422
        upd.assert_not_called()

    def test_401(self, api_client):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=None):
            resp = api_client.put("/api/user/icp-definition", json={"session_token": "x"})
        assert resp.status_code == 401

    def test_write_failure_is_500(self, api_client):
        resp, _ = self._put(api_client, {"icp_builder_enabled": True}, ok=False)
        assert resp.status_code == 500


class TestPrefill:
    def test_seed_urls_take_icp_roster_and_best_leads_only(self):
        urls = seed_profile_urls(
            [{"category": "icp", "profile_url": "https://x/in/a"},
             {"category": "peer", "profile_url": "https://x/in/b"}],
            [{"stage": "hot", "person_profile_url": "https://x/in/c/"},
             {"stage": "cold", "person_profile_url": "https://x/in/d"},
             {"stage": "cold", "manual_stage": "opportunity", "person_profile_url": "https://x/in/A"}])
        assert urls == ["https://x/in/a", "https://x/in/c/"]

    def test_draft_ranks_common_titles_and_ignores_strangers(self):
        facts = {
            "https://x/in/a": {"job_title": "CTO", "industry": "Software"},
            "https://x/in/c": {"job_title": "cto", "industry": "null"},
            "https://x/in/zzz": {"job_title": "Plumber", "industry": "Trades"},
            "https://x/in/e": {"job_title": None, "industry": None},
        }
        out = draft_icp_definition(["https://x/in/a", "https://x/in/c", "https://x/in/e"], facts)
        segment = out["definition"]["segments"][0]
        assert segment["role_titles"] == ["CTO"]
        assert segment["industries"] == ["Software"]
        assert out["sample_size"] == 2

    def test_no_facts_is_none(self):
        assert draft_icp_definition(["https://x/in/a"], {}) is None
