"""Phase 2 of #2702: API follower/connection counts — client parsing, gating, capture wiring."""
from unittest.mock import MagicMock, patch

import requests

from cqc_lem.utilities.linkedin import member_post_analytics as mpa

_M = "cqc_lem.utilities.linkedin.member_post_analytics"
_ENV = "cqc_lem.utilities.env_constants"
_POST = "cqc_lem.app.engagement.posting"


def _resp(status=200, payload=None):
    r = MagicMock()
    r.status_code = status
    r.json.return_value = payload
    return r


class TestClient:
    def test_follower_count_parsed(self):
        with patch(f"{_M}.requests.get",
                   return_value=_resp(payload={"elements": [{"memberFollowersCount": 321}]})) as get:
            assert mpa.fetch_member_follower_count("tok", "202606") == 321
        assert get.call_args[0][0].endswith("memberFollowersCount?q=me")
        assert get.call_args.kwargs["headers"]["Authorization"] == "Bearer tok"

    def test_follower_count_unreadable_is_none(self):
        for payload in ({}, {"elements": []}, {"elements": [{"memberFollowersCount": True}]},
                        {"elements": [{"memberFollowersCount": -1}]}, ["x"]):
            with patch(f"{_M}.requests.get", return_value=_resp(payload=payload)):
                assert mpa.fetch_member_follower_count("tok", "202606") is None

    def test_non_200_and_transport_error_are_none(self):
        with patch(f"{_M}.requests.get", return_value=_resp(status=403)):
            assert mpa.fetch_member_follower_count("tok", "202606") is None
        with patch(f"{_M}.requests.get", side_effect=requests.ConnectionError()):
            assert mpa.fetch_member_connection_count("abc", "tok", "202606") is None
        bad = _resp()
        bad.json.side_effect = ValueError()
        with patch(f"{_M}.requests.get", return_value=bad):
            assert mpa.fetch_member_follower_count("tok", "202606") is None

    def test_connection_count_parsed_and_urn_encoded(self):
        with patch(f"{_M}.requests.get",
                   return_value=_resp(payload={"firstDegreeSize": 480})) as get:
            assert mpa.fetch_member_connection_count("abc", "tok", "202606") == 480
        assert "urn%3Ali%3Aperson%3Aabc" in get.call_args[0][0]

    def test_connection_count_needs_person_id(self):
        with patch(f"{_M}.requests.get") as get:
            assert mpa.fetch_member_connection_count("", "tok", "202606") is None
        get.assert_not_called()

    def test_demo_mode_sends_nothing(self):
        with patch(f"{_M}.guard_linkedin", side_effect=mpa.DemoModeError("demo")), \
             patch(f"{_M}.requests.get") as get:
            assert mpa.fetch_member_follower_count("tok", "202606") is None
        get.assert_not_called()


class TestGating:
    def _on(self, scope, flag=True):
        return (patch(f"{_ENV}.LI_MEMBER_AUDIENCE_API_ENABLED", flag),
                patch(f"{_M}.member_post_analytics_enabled_for_user", return_value=True),
                patch(f"{_M}.member_post_analytics_separate_app_enabled", return_value=True),
                patch(f"{_M}.get_member_post_analytics_access_token", return_value="tok"),
                patch("cqc_lem.utilities.db.get_member_analytics_token_info",
                      return_value={"granted_scope": scope, "linked_sub_id": "abc"}),
                patch(f"{_M}.fetch_member_follower_count", return_value=10),
                patch(f"{_M}.fetch_member_connection_count", return_value=20))

    def _run(self, scope, flag=True):
        ps = self._on(scope, flag)
        for p in ps:
            p.start()
        try:
            return mpa.member_audience_counts(1)
        finally:
            for p in ps:
                p.stop()

    def test_flag_off_reads_nothing(self):
        assert self._run("r_member_profileAnalytics r_1st_connections_size", flag=False) == {
            "follower_count": None, "connection_count": None}

    def test_both_scopes_granted(self):
        assert self._run("r_member_profileAnalytics r_1st_connections_size") == {
            "follower_count": 10, "connection_count": 20}

    def test_missing_scope_is_silent_fallback(self):
        assert self._run("r_member_postAnalytics") == {
            "follower_count": None, "connection_count": None}
        assert self._run("r_member_profileAnalytics") == {
            "follower_count": 10, "connection_count": None}

    def test_no_stored_scope_or_token_reads_nothing(self):
        assert self._run(None)["follower_count"] is None


class TestCaptureWiring:
    def _go(self, counts, api):
        from cqc_lem.app.engagement.posting import capture_follower_stats
        with patch(f"{_POST}.get_current_profile",
                   return_value=(MagicMock(), MagicMock(), "e@x.com", MagicMock())), \
             patch(f"{_POST}.get_linkedin_profile_url_by_user_id", return_value="u"), \
             patch(f"{_POST}.capture_audience_snapshot", return_value=dict(counts)), \
             patch(f"{_POST}.quit_gracefully"), \
             patch(f"{_M}.member_audience_counts", **api), \
             patch(f"{_POST}.record_follower_stat", return_value=True) as rec, \
             patch(f"{_POST}.track_audience_snapshot") as track:
            capture_follower_stats.run(user_id=1)
        return rec, track

    _SCRAPED = {"follower_count": 100, "connection_count": 50,
                "profile_views": 5, "search_appearances": 2}

    def test_api_value_preferred_and_tagged(self):
        rec, track = self._go(self._SCRAPED, {"return_value": {"follower_count": 123,
                                                               "connection_count": None}})
        assert rec.call_args.kwargs["follower_count"] == 123
        assert rec.call_args.kwargs["follower_source"] == "api"
        assert rec.call_args.kwargs["connection_count"] == 50
        assert "connection_source" not in rec.call_args.kwargs
        assert track.call_args.kwargs["follower_source"] == "api"

    def test_api_failure_falls_back_to_scrape(self):
        rec, _ = self._go(self._SCRAPED, {"side_effect": RuntimeError("boom")})
        assert rec.call_args.kwargs == self._SCRAPED

    def test_api_counts_never_reach_posthog(self):
        from cqc_lem.utilities import observability
        with patch.object(observability, "_emit") as emit:
            observability.track_audience_snapshot(1, follower_count=123, connection_count=50,
                                                  follower_source="api")
        assert emit.call_args[0][1]["follower_count"] is None
        assert emit.call_args[0][1]["connection_count"] == 50
