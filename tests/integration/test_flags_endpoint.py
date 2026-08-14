"""Integration test for the SPA's feature-flag bootstrap endpoint (issue #651).

Drives the real GET /api/flags handler through the real utilities/flags.py resolution, so the
payload the browser bootstraps from is provably the SAME evaluation the API and the Celery workers
just did — including the fail-open-to-env-var path, which is the whole contract.
"""
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest

from cqc_lem.utilities import flags

pytestmark = pytest.mark.integration

_M = "cqc_lem.api.main"


@pytest.fixture(autouse=True)
def _clean_flag_state(monkeypatch):
    for name in ("POSTHOG_FLAGS_ENABLED", "POSTHOG_API_KEY", "POSTHOG_PERSONAL_API_KEY",
                 "COMMENT_RESEARCH_ENABLED", "TUTORIAL_VIDEOS_ENABLED",
                 "FEED_FALLBACK_WHEN_EMPTY_DEFAULT", "COST_ROUTING_ENABLED"):
        monkeypatch.delenv(name, raising=False)
    flags.reset_flag_state()
    yield
    flags.reset_flag_state()


@contextmanager
def _posthog_says(value):
    with patch("cqc_lem.utilities.flags.posthog") as ph:
        ph.disabled = False
        ph.setup.return_value = MagicMock()
        ph.get_feature_flag.return_value = value
        yield ph


def _get(api_client, url):
    with patch("cqc_lem.utilities.observability.posthog"):
        return api_client.get(url)


class TestFlagBootstrap:
    def test_returns_every_registered_flag(self, api_client):
        response = _get(api_client, "/api/flags")

        assert response.status_code == 200
        detail = response.json()["detail"]
        assert set(detail["flags"]) == set(flags.FLAGS)
        assert all(isinstance(v, bool) for v in detail["flags"].values())

    def test_without_posthog_the_payload_is_the_env_defaults(self, api_client, monkeypatch):
        """The landing page must render correctly on a deployment with no PostHog at all."""
        monkeypatch.setenv("TUTORIAL_VIDEOS_ENABLED", "true")

        detail = _get(api_client, "/api/flags").json()["detail"]

        assert detail["local_evaluation"] is False
        assert detail["flags"][flags.TUTORIAL_VIDEOS] is True
        assert detail["flags"][flags.COMMENT_RESEARCH] is False

    def test_no_session_resolves_the_system_identity_rather_than_401ing(self, api_client):
        detail = _get(api_client, "/api/flags").json()["detail"]

        assert detail["distinct_id"] == "system"

    def test_an_invalid_session_still_serves_flags(self, api_client):
        with patch(f"{_M}.get_session_user_id", return_value=None):
            detail = _get(api_client, "/api/flags?session_token=stale").json()["detail"]

        assert detail["distinct_id"] == "system"
        assert set(detail["flags"]) == set(flags.FLAGS)

    def test_a_valid_session_scopes_the_payload_to_that_user(self, api_client, monkeypatch):
        monkeypatch.setenv("POSTHOG_API_KEY", "phc_test")
        monkeypatch.setenv("POSTHOG_PERSONAL_API_KEY", "phx_test")

        with patch(f"{_M}.get_session_user_id", return_value=42), _posthog_says(True) as ph:
            detail = _get(api_client, "/api/flags?session_token=good").json()["detail"]

        # distinct_id is str(user_id) — observability.py's convention, so a per-user rollout targets
        # the same PostHog person that user's own events land on.
        assert detail["distinct_id"] == "42"
        assert ph.get_feature_flag.call_args.args[1] == "42"
        assert detail["local_evaluation"] is True
        assert all(detail["flags"].values())

    def test_stays_outside_the_bearer_gate_for_a_logged_out_visitor(self, api_client):
        """The landing page bootstraps its flags from here and carries no bearer token. If the gate
        covered this path the query would 401 — and the SPA's axios interceptor reads ANY 401 as a
        dead session, clearing lem_session and redirecting, so a signed-in visitor landing on `/`
        would be logged out by a marketing section.
        """
        from cqc_lem.api.main import _PUBLIC_API_PREFIXES, _api_token_required

        assert "/api/flags" in _PUBLIC_API_PREFIXES
        with patch(f"{_M}._API_ACCESS_TOKEN_SET", {"secret"}):
            assert _api_token_required("/api/flags") is False
            # The exact leaf only — the boundary rule must not open a future /api/flags-admin.
            assert _api_token_required("/api/flags-admin") is True
            response = _get(api_client, "/api/flags")

        assert response.status_code == 200

    def test_posthog_decision_beats_the_env_var_in_the_payload(self, api_client, monkeypatch):
        monkeypatch.setenv("POSTHOG_API_KEY", "phc_test")
        monkeypatch.setenv("POSTHOG_PERSONAL_API_KEY", "phx_test")
        monkeypatch.setenv("TUTORIAL_VIDEOS_ENABLED", "true")

        with _posthog_says(False):
            detail = _get(api_client, "/api/flags").json()["detail"]

        assert detail["flags"][flags.TUTORIAL_VIDEOS] is False
