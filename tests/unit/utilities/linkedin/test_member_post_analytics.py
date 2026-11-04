"""Unit tests for the allowlisted Community Management member analytics client."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import requests

from cqc_lem.utilities import env_constants
from cqc_lem.utilities.demo_mode import DemoModeError
from cqc_lem.utilities.linkedin import member_post_analytics as analytics

pytestmark = pytest.mark.unit


def _payload(metric: str, count: int) -> dict:
    return {"elements": [{"metricType": metric, "count": count}]}


class TestAllowlistAndAppMode:
    def test_global_switch_and_user_allowlist_both_gate_collection(self, monkeypatch):
        monkeypatch.setattr(env_constants, "LI_MEMBER_POST_ANALYTICS_ENABLED", True)
        monkeypatch.setattr(env_constants, "LI_MEMBER_POST_ANALYTICS_USER_IDS", frozenset({1}))

        assert analytics.member_post_analytics_enabled_for_user(1)
        assert not analytics.member_post_analytics_enabled_for_user(2)
        assert not analytics.member_post_analytics_enabled_for_user(None)

        monkeypatch.setattr(env_constants, "LI_MEMBER_POST_ANALYTICS_ENABLED", False)
        assert not analytics.member_post_analytics_enabled_for_user(1)

    def test_separate_app_connection_requires_allowlist_and_all_credentials(self, monkeypatch):
        monkeypatch.setattr(env_constants, "LI_MEMBER_POST_ANALYTICS_USER_IDS", frozenset({1}))
        monkeypatch.setattr(env_constants, "LI_MEMBER_POST_ANALYTICS_SEPARATE_APP_ENABLED", True)
        monkeypatch.setattr(env_constants, "LI_MEMBER_POST_ANALYTICS_CLIENT_ID", "client")
        monkeypatch.setattr(env_constants, "LI_MEMBER_POST_ANALYTICS_CLIENT_SECRET", "secret")
        monkeypatch.setattr(env_constants, "LI_MEMBER_POST_ANALYTICS_REDIRECT_URL", "https://lem.test/callback")

        assert analytics.member_post_analytics_connection_available_for_user(1)
        assert not analytics.member_post_analytics_connection_available_for_user(2)

        monkeypatch.setattr(env_constants, "LI_MEMBER_POST_ANALYTICS_CLIENT_SECRET", "")
        assert not analytics.member_post_analytics_connection_available_for_user(1)

    def test_token_lookup_never_crosses_app_boundary(self, monkeypatch):
        from cqc_lem.utilities import db

        member_token = Mock(return_value="cma-token")
        publish_token = Mock(return_value="lem-token")
        monkeypatch.setattr(db, "get_member_analytics_access_token", member_token)
        monkeypatch.setattr(db, "get_user_access_token", publish_token)
        monkeypatch.setattr(analytics, "member_post_analytics_separate_app_enabled", lambda: True)

        assert analytics.get_member_post_analytics_access_token(1) == "cma-token"
        member_token.assert_called_once_with(1)
        publish_token.assert_not_called()

        monkeypatch.setattr(analytics, "member_post_analytics_separate_app_enabled", lambda: False)
        assert analytics.get_member_post_analytics_access_token(1) == "lem-token"
        publish_token.assert_called_once_with(1)


class TestEntityAndMetricParsing:
    def test_share_and_ugc_post_urns_use_finder_keys(self):
        assert analytics._entity_from_post_url("https://linkedin.test/urn:li:share:123") == (
            "(share:urn%3Ali%3Ashare%3A123)")
        assert analytics._entity_from_post_url("https://linkedin.test/urn:li:ugcPost:456") == (
            "(ugc:urn%3Ali%3AugcPost%3A456)")

    def test_non_post_urn_is_rejected(self):
        assert analytics._entity_from_post_url("https://linkedin.test/urn:li:activity:123") is None
        assert analytics._entity_from_post_url("") is None

    def test_metric_parser_accepts_string_and_legacy_object_shapes(self):
        assert analytics._metric_count(_payload("REACTION", 3), "REACTION") == 3
        legacy = {"elements": [{"metricType": {"com.linkedin.metric": "REACTION"}, "count": 5}]}
        assert analytics._metric_count(legacy, "REACTION") == 5

    @pytest.mark.parametrize("payload", [None, {}, {"elements": "bad"},
                                         {"elements": [{"metricType": "COMMENT", "count": 4}]},
                                         {"elements": [{"metricType": "REACTION", "count": True}]},
                                         {"elements": [{"metricType": "REACTION", "count": "4"}]}])
    def test_missing_or_invalid_metric_is_not_zero(self, payload):
        assert analytics._metric_count(payload, "REACTION") is None


class TestFetchMemberPostAnalytics:
    def test_fetches_all_metrics_with_version_and_restli_headers(self, monkeypatch):
        monkeypatch.setattr(analytics, "guard_linkedin", lambda _surface: None)
        calls = []

        def get(url, headers, timeout):
            calls.append((url, headers, timeout))
            metric = analytics._METRICS[len(calls) - 1][0]
            return SimpleNamespace(status_code=200, json=lambda: _payload(metric, len(calls)))

        monkeypatch.setattr(analytics.requests, "get", get)
        counts, failure = analytics.fetch_member_post_analytics(
            "https://linkedin.test/urn:li:share:123", "private-token", "202606")

        assert failure is None
        assert counts == {column: index for index, (_, column, _) in enumerate(analytics._METRICS, 1)}
        assert len(calls) == len(analytics._METRICS)
        assert calls[0][0].endswith("queryType=IMPRESSION&aggregation=TOTAL")
        assert calls[0][1] == {
            "Authorization": "Bearer private-token",
            "LinkedIn-Version": "202606",
            "X-Restli-Protocol-Version": "2.0.0",
        }
        assert calls[0][2] == 20

    def test_forbidden_response_stops_metric_requests_without_echoing_token(self, monkeypatch):
        monkeypatch.setattr(analytics, "guard_linkedin", lambda _surface: None)
        get = Mock(return_value=SimpleNamespace(status_code=403))
        monkeypatch.setattr(analytics.requests, "get", get)

        counts, failure = analytics.fetch_member_post_analytics(
            "https://linkedin.test/urn:li:share:123", "private-token", "202606")

        assert counts == {}
        assert failure == "http_403"
        assert get.call_count == 1
        assert "private-token" not in failure

    def test_old_version_stops_before_network_call(self, monkeypatch):
        get = Mock()
        monkeypatch.setattr(analytics.requests, "get", get)

        counts, failure = analytics.fetch_member_post_analytics(
            "https://linkedin.test/urn:li:share:123", "token", "202503")

        assert counts == {}
        assert failure == "unsupported_version"
        get.assert_not_called()

    def test_demo_mode_and_transport_error_fail_closed(self, monkeypatch):
        def reject(_surface):
            raise DemoModeError("demo")

        monkeypatch.setattr(analytics, "guard_linkedin", reject)
        get = Mock()
        monkeypatch.setattr(analytics.requests, "get", get)
        assert analytics.fetch_member_post_analytics(
            "https://linkedin.test/urn:li:share:123", "token", "202606") == ({}, "demo_mode")
        get.assert_not_called()

        monkeypatch.setattr(analytics, "guard_linkedin", lambda _surface: None)
        get.side_effect = requests.Timeout
        assert analytics.fetch_member_post_analytics(
            "https://linkedin.test/urn:li:share:123", "token", "202606") == ({}, "transport_error")

    def test_partial_metric_shape_is_reported(self, monkeypatch):
        monkeypatch.setattr(analytics, "guard_linkedin", lambda _surface: None)
        monkeypatch.setattr(
            analytics.requests, "get",
            lambda *_args, **_kwargs: SimpleNamespace(status_code=200, json=lambda: {"elements": []}))

        counts, failure = analytics.fetch_member_post_analytics(
            "https://linkedin.test/urn:li:share:123", "token", "202606")

        assert counts == {}
        assert failure == "partial_response"
