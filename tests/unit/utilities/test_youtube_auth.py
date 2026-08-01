"""Unit tests for the YouTube OAuth refresh-token lifecycle (issue #742).

The token endpoint, the DB credential store, Redis and email are all mocked, so every state the
probe can reach is exercised in-process. The tests that matter most are the ones pinning what does
NOT alert: an unconfigured install and an unreachable Google are not "publishing is broken", and a
probe that cried wolf on either would train the owner to ignore the one alert that counts.
"""

import json
from unittest.mock import MagicMock, patch

import pytest

from cqc_lem.utilities.marketing import youtube_auth as ya

pytestmark = pytest.mark.unit

_MOD = "cqc_lem.utilities.marketing.youtube_auth"


class _FakeRedis:
    def __init__(self):
        self.store = {}

    def get(self, key):
        return self.store.get(key)

    def set(self, key, value):
        self.store[key] = value


def _response(status_code=200, payload=None):
    response = MagicMock()
    response.status_code = status_code
    response.json.return_value = payload if payload is not None else {}
    return response


@pytest.fixture(autouse=True)
def _configured(monkeypatch):
    """Credentials present, nothing stored in the DB, an in-memory Redis."""
    monkeypatch.setattr(ya, "YOUTUBE_CLIENT_ID", "cid")
    monkeypatch.setattr(ya, "YOUTUBE_CLIENT_SECRET", "secret")
    monkeypatch.setattr(ya, "YOUTUBE_REFRESH_TOKEN", "env-token")
    monkeypatch.setattr(ya, "YOUTUBE_ALERT_EMAIL", "owner@lem.test")
    monkeypatch.setattr(ya, "get_app_credential", lambda name: None)
    monkeypatch.setattr(ya, "get_app_credential_updated_at", lambda name: None)
    monkeypatch.setattr(ya, "set_app_credential", lambda *a, **kw: True)
    redis = _FakeRedis()
    monkeypatch.setattr(ya, "_redis", lambda: redis)
    return redis


class TestTokenSource:
    def test_db_value_wins_over_the_env_seed(self, monkeypatch):
        monkeypatch.setattr(ya, "get_app_credential", lambda name: "db-token")
        assert ya.refresh_token() == "db-token"
        assert ya.token_source() == "db"

    def test_env_is_the_fallback_when_nothing_is_stored(self):
        assert ya.refresh_token() == "env-token"
        assert ya.token_source() == "env"

    def test_a_db_outage_falls_back_to_env_instead_of_looking_unconfigured(self, monkeypatch):
        def _boom(name):
            raise RuntimeError("db down")

        monkeypatch.setattr(ya, "get_app_credential", _boom)
        assert ya.refresh_token() == "env-token"
        assert ya.youtube_configured() is True

    def test_no_token_anywhere_is_not_configured(self, monkeypatch):
        monkeypatch.setattr(ya, "YOUTUBE_REFRESH_TOKEN", "")
        assert ya.refresh_token() == ""
        assert ya.token_source() == "none"
        assert ya.youtube_configured() is False

    def test_storing_an_empty_token_is_rejected(self):
        assert ya.store_refresh_token("   ") is False

    def test_probe_reads_the_db_token_not_the_env_seed(self, monkeypatch):
        monkeypatch.setattr(ya, "get_app_credential", lambda name: "db-token")
        with patch("requests.post", return_value=_response(
                payload={"access_token": "at", "scope": ya.UPLOAD_SCOPE})) as post:
            assert ya.probe()["status"] == ya.STATUS_OK
        assert post.call_args.kwargs["data"]["refresh_token"] == "db-token"


class TestProbe:
    def test_success_reports_ok_and_the_granted_scope(self, _configured):
        with patch("requests.post", return_value=_response(
                payload={"access_token": "at", "scope": f"{ya.UPLOAD_SCOPE} openid"})):
            state = ya.probe()
        assert state["status"] == ya.STATUS_OK
        assert ya.UPLOAD_SCOPE in state["scope"]
        # The last state is persisted with no TTL — it is the audit trail.
        assert json.loads(_configured.store[ya._STATE_KEY])["status"] == ya.STATUS_OK

    def test_a_response_that_omits_scope_is_still_ok(self):
        with patch("requests.post", return_value=_response(payload={"access_token": "at"})):
            state = ya.probe()
        assert state["status"] == ya.STATUS_OK and state["scope"] is None

    def test_invalid_grant_needs_reauth_and_names_the_error(self):
        with patch("requests.post", return_value=_response(
                400, {"error": "invalid_grant", "error_description": "Token has been expired"})):
            state = ya.probe()
        assert state["status"] == ya.STATUS_NEEDS_REAUTH
        assert state["error"] == "invalid_grant"
        assert "expired" in state["reason"]

    def test_invalid_client_needs_reauth(self):
        with patch("requests.post", return_value=_response(401, {"error": "invalid_client"})):
            assert ya.probe()["status"] == ya.STATUS_NEEDS_REAUTH

    def test_lost_upload_scope_needs_reauth_even_though_the_token_still_mints(self):
        with patch("requests.post", return_value=_response(
                payload={"access_token": "at",
                         "scope": "https://www.googleapis.com/auth/youtube.readonly"})):
            state = ya.probe()
        assert state["status"] == ya.STATUS_NEEDS_REAUTH
        assert state["error"] == "scope_missing"

    def test_a_200_without_an_access_token_needs_reauth(self):
        with patch("requests.post", return_value=_response(payload={})):
            assert ya.probe()["error"] == "no_access_token"

    def test_a_server_error_is_undecidable_not_broken(self):
        with patch("requests.post", return_value=_response(503, {})):
            assert ya.probe()["status"] == ya.STATUS_UNKNOWN

    def test_throttling_is_undecidable_not_broken(self):
        with patch("requests.post", return_value=_response(429, {})):
            assert ya.probe()["status"] == ya.STATUS_UNKNOWN

    def test_an_unreachable_endpoint_is_undecidable_not_broken(self):
        with patch("requests.post", side_effect=OSError("connection reset")):
            state = ya.probe()
        assert state["status"] == ya.STATUS_UNKNOWN and "unreachable" in state["reason"]

    def test_missing_credentials_never_calls_google(self, monkeypatch):
        monkeypatch.setattr(ya, "YOUTUBE_CLIENT_ID", "")
        with patch("requests.post") as post:
            assert ya.probe()["status"] == ya.STATUS_NOT_CONFIGURED
        post.assert_not_called()

    def test_missing_refresh_token_never_calls_google(self, monkeypatch):
        monkeypatch.setattr(ya, "YOUTUBE_REFRESH_TOKEN", "")
        with patch("requests.post") as post:
            assert ya.probe()["status"] == ya.STATUS_NOT_CONFIGURED
        post.assert_not_called()

    def test_a_rotated_refresh_token_is_persisted(self, monkeypatch):
        stored = {}
        monkeypatch.setattr(ya, "set_app_credential",
                            lambda name, value, note=None: stored.update({name: value}) or True)
        with patch("requests.post", return_value=_response(
                payload={"access_token": "at", "refresh_token": "rotated"})):
            ya.probe()
        assert stored[ya.CREDENTIAL_NAME] == "rotated"

    def test_persist_false_leaves_the_weekly_audit_record_untouched(self, _configured):
        with patch("requests.post", return_value=_response(payload={"access_token": "at"})):
            ya.probe(persist=False)
        assert ya._STATE_KEY not in _configured.store

    def test_no_redis_still_returns_a_state(self, monkeypatch):
        monkeypatch.setattr(ya, "_redis", lambda: None)
        with patch("requests.post", return_value=_response(payload={"access_token": "at"})):
            assert ya.probe()["status"] == ya.STATUS_OK
        assert ya.last_probe() is None


class TestMintAccessToken:
    def test_returns_the_access_token(self):
        response = _response(payload={"access_token": "at"})
        with patch("requests.post", return_value=response):
            assert ya.mint_access_token() == "at"
        response.raise_for_status.assert_called_once()

    def test_a_response_without_a_token_raises(self):
        with patch("requests.post", return_value=_response(payload={})):
            with pytest.raises(RuntimeError, match="no access_token"):
                ya.mint_access_token()


class TestPreflight:
    def test_only_a_dead_grant_aborts_a_run(self):
        with patch("requests.post", return_value=_response(400, {"error": "invalid_grant"})):
            assert ya.preflight()["should_abort"] is True

    def test_an_undecidable_probe_does_not_abort_a_run(self):
        with patch("requests.post", side_effect=OSError("down")):
            assert ya.preflight()["should_abort"] is False

    def test_an_unconfigured_install_does_not_abort_a_run(self, monkeypatch):
        monkeypatch.setattr(ya, "YOUTUBE_CLIENT_ID", "")
        assert ya.preflight()["should_abort"] is False


class TestHealthProbe:
    def test_ok_logs_the_dated_audit_line_and_emails_nobody(self):
        with patch("requests.post", return_value=_response(
                payload={"access_token": "at", "scope": ya.UPLOAD_SCOPE})), \
             patch("cqc_lem.utilities.email._dispatch_email") as send, \
             patch(f"{_MOD}.log_info") as info, \
             patch("cqc_lem.utilities.observability.track_youtube_token_check") as track:
            state = ya.run_health_probe()
        assert state["status"] == ya.STATUS_OK and state["emailed"] is False
        assert state["tracked"] is True
        send.assert_not_called()
        assert "checked" in info.call_args[0][0]
        assert track.call_args[0][0]["status"] == ya.STATUS_OK

    def test_needs_reauth_emails_the_owner_with_the_error_and_the_runbook(self):
        with patch("requests.post", return_value=_response(400, {"error": "invalid_grant"})), \
             patch("cqc_lem.utilities.email._dispatch_email", return_value=True) as send, \
             patch("cqc_lem.utilities.observability.track_youtube_token_check"):
            state = ya.run_health_probe()
        assert state["status"] == ya.STATUS_NEEDS_REAUTH and state["emailed"] is True
        recipient, subject = send.call_args[0][0], send.call_args[0][1]
        body = send.call_args.kwargs["text_content"]
        assert recipient == "owner@lem.test" and "re-auth" in subject.lower()
        assert "invalid_grant" in body and "Internal" in body

    def test_scope_loss_alerts_too(self):
        with patch("requests.post", return_value=_response(
                payload={"access_token": "at", "scope": "openid"})), \
             patch("cqc_lem.utilities.email._dispatch_email", return_value=True) as send, \
             patch("cqc_lem.utilities.observability.track_youtube_token_check"):
            assert ya.run_health_probe()["emailed"] is True
        send.assert_called_once()

    def test_an_undecidable_probe_alerts_nobody(self):
        with patch("requests.post", side_effect=OSError("down")), \
             patch("cqc_lem.utilities.email._dispatch_email") as send, \
             patch("cqc_lem.utilities.observability.track_youtube_token_check"):
            state = ya.run_health_probe()
        assert state["status"] == ya.STATUS_UNKNOWN and state["emailed"] is False
        send.assert_not_called()

    def test_an_unconfigured_install_is_a_debug_no_op_not_a_warning(self, monkeypatch):
        monkeypatch.setattr(ya, "YOUTUBE_CLIENT_ID", "")
        with patch("cqc_lem.utilities.email._dispatch_email") as send, \
             patch(f"{_MOD}.log_warning") as warn, \
             patch(f"{_MOD}.log_debug") as debug, \
             patch("cqc_lem.utilities.observability.track_youtube_token_check"):
            state = ya.run_health_probe()
        assert state["status"] == ya.STATUS_NOT_CONFIGURED
        send.assert_not_called()
        warn.assert_not_called()
        debug.assert_called_once()

    def test_no_recipient_configured_does_not_crash_the_beat(self, monkeypatch):
        monkeypatch.setattr(ya, "YOUTUBE_ALERT_EMAIL", "")
        monkeypatch.setattr(ya, "MARGIN_REPORT_EMAIL", "")
        with patch("requests.post", return_value=_response(400, {"error": "invalid_grant"})), \
             patch("cqc_lem.utilities.observability.track_youtube_token_check"):
            assert ya.run_health_probe()["emailed"] is False

    def test_an_email_failure_does_not_crash_the_beat(self):
        with patch("requests.post", return_value=_response(400, {"error": "invalid_grant"})), \
             patch("cqc_lem.utilities.email._dispatch_email", side_effect=RuntimeError("smtp")), \
             patch("cqc_lem.utilities.observability.track_youtube_token_check"):
            assert ya.run_health_probe()["emailed"] is False

    def test_an_analytics_failure_does_not_crash_the_beat(self):
        with patch("requests.post", return_value=_response(payload={"access_token": "at"})), \
             patch("cqc_lem.utilities.observability.track_youtube_token_check",
                   side_effect=RuntimeError("posthog")):
            assert ya.run_health_probe()["tracked"] is False


class TestStatusReport:
    def test_reads_the_recorded_probe_without_calling_google(self, _configured):
        _configured.store[ya._STATE_KEY] = json.dumps(
            {"status": ya.STATUS_OK, "reason": "fine", "checked_at": "2026-08-01T00:00:00+00:00",
             "scope": ya.UPLOAD_SCOPE})
        with patch("requests.post") as post:
            report = ya.status_report()
        post.assert_not_called()
        assert report["connected"] is True and report["configured"] is True
        assert report["checked_at"] == "2026-08-01T00:00:00+00:00"

    def test_live_reprobes_even_when_a_state_is_recorded(self, _configured):
        _configured.store[ya._STATE_KEY] = json.dumps({"status": ya.STATUS_NEEDS_REAUTH})
        with patch("requests.post", return_value=_response(payload={"access_token": "at"})) as post:
            report = ya.status_report(live=True)
        post.assert_called_once()
        assert report["connected"] is True

    def test_probes_once_when_nothing_has_been_recorded_yet(self, _configured):
        with patch("requests.post", return_value=_response(400, {"error": "invalid_grant"})):
            report = ya.status_report()
        assert report["connected"] is False and report["status"] == ya.STATUS_NEEDS_REAUTH
        assert report["error"] == "invalid_grant"

    def test_never_returns_the_refresh_token_itself(self, _configured):
        with patch("requests.post", return_value=_response(payload={"access_token": "at"})):
            report = ya.status_report()
        assert "env-token" not in json.dumps(report)

    def test_a_corrupt_recorded_state_falls_back_to_a_live_probe(self, _configured):
        _configured.store[ya._STATE_KEY] = "{not json"
        with patch("requests.post", return_value=_response(payload={"access_token": "at"})):
            assert ya.status_report()["connected"] is True
