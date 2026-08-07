"""Unit tests for the /api/user/location endpoints (get / put / autocapture)."""

from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.unit


@pytest.fixture(scope="module")
def client():
    patches = [
        patch("cqc_lem.utilities.observability.track_api_call"),
        patch("cqc_lem.app.run_automation.automate_invites_to_company_page_for_user"),
        patch("cqc_lem.app.run_automation.automate_reply_commenting"),
        patch("cqc_lem.app.run_content_plan.auto_create_weekly_content"),
        patch("cqc_lem.app.aws_test_celery_task.test_get_my_profile"),
    ]
    for p in patches:
        p.start()
    try:
        from fastapi.testclient import TestClient

        from cqc_lem.api.main import app
        with TestClient(app, raise_server_exceptions=False) as tc:
            yield tc
    finally:
        for p in patches:
            p.stop()


_SESSION = "tok_test"
_USER_ID = 42


class TestGetLocation:
    def test_returns_geo(self, client):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=_USER_ID), \
             patch("cqc_lem.api.main.get_user_geo", return_value={"latitude": 1.0, "longitude": 2.0}):
            resp = client.get(f"/api/user/location?session_token={_SESSION}")
        assert resp.status_code == 200
        assert resp.json()["detail"]["latitude"] == 1.0

    def test_401_invalid_session(self, client):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=None):
            resp = client.get("/api/user/location?session_token=bad")
        assert resp.status_code == 401


class TestPutLocation:
    def test_updates_valid_location(self, client):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=_USER_ID), \
             patch("cqc_lem.api.main.update_user_location", return_value=True) as upd:
            resp = client.put("/api/user/location", json={
                "session_token": _SESSION, "latitude": 40.0, "longitude": -73.0,
                "city": "NYC", "country": "US", "locale": "en-US",
            })
        assert resp.status_code == 200
        assert upd.called

    def test_rejects_out_of_range_coords(self, client):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=_USER_ID):
            resp = client.put("/api/user/location", json={
                "session_token": _SESSION, "latitude": 200.0, "longitude": 0.0,
            })
        assert resp.status_code == 422

    def test_rejects_bad_country_code(self, client):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=_USER_ID):
            resp = client.put("/api/user/location", json={
                "session_token": _SESSION, "latitude": 1.0, "longitude": 2.0, "country": "USA",
            })
        assert resp.status_code == 422

    def test_401_invalid_session(self, client):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=None):
            resp = client.put("/api/user/location", json={
                "session_token": "bad", "latitude": 1.0, "longitude": 2.0,
            })
        assert resp.status_code == 401


class TestAutocaptureLocation:
    def _ipapi_response(self):
        m = MagicMock()
        m.raise_for_status.return_value = None
        m.json.return_value = {
            "latitude": 40.71, "longitude": -74.0, "city": "New York",
            "country_code": "US", "timezone": "America/New_York", "languages": "en-US,es",
        }
        return m

    def test_captures_from_client_ip_header(self, client):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=_USER_ID), \
             patch("cqc_lem.api.main.update_user_location", return_value=True) as upd, \
             patch("cqc_lem.api.main.requests.get", return_value=self._ipapi_response()) as rget:
            resp = client.post(
                "/api/user/location/autocapture",
                json={"session_token": _SESSION},
                headers={"CF-Connecting-IP": "203.0.113.7"},
            )
        assert resp.status_code == 200
        assert "203.0.113.7" in rget.call_args[0][0]
        # source must be ip_autocapture
        assert upd.call_args.kwargs["source"] == "ip_autocapture"
        assert resp.json()["detail"]["city"] == "New York"

    def test_502_when_geolocation_fails(self, client):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=_USER_ID), \
             patch("cqc_lem.api.main.requests.get", side_effect=Exception("boom")):
            resp = client.post(
                "/api/user/location/autocapture",
                json={"session_token": _SESSION},
                headers={"X-Forwarded-For": "203.0.113.9"},
            )
        assert resp.status_code == 502

    def test_401_invalid_session(self, client):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=None):
            resp = client.post("/api/user/location/autocapture", json={"session_token": "bad"})
        assert resp.status_code == 401


class TestLocationByCity:
    BASE = "/api/user/location/by-city"
    GEO = {"latitude": 28.5384, "longitude": -81.3789, "city": "Orlando",
           "country": "US", "timezone": "America/New_York", "locale": "en-US"}

    def test_geocodes_and_saves_manual(self, client):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=_USER_ID), \
             patch("cqc_lem.api.main.geocode_city", return_value=self.GEO), \
             patch("cqc_lem.api.main.update_user_location", return_value=True) as save:
            resp = client.post(self.BASE, json={"session_token": _SESSION, "city": "Orlando", "state": "Florida"})
        assert resp.status_code == 200
        assert resp.json()["detail"]["timezone"] == "America/New_York"
        assert save.call_args.kwargs["source"] == "manual"

    def test_bad_city_returns_422(self, client):
        from cqc_lem.utilities.geocoding import GeocodeError
        with patch("cqc_lem.api.main.get_session_user_id", return_value=_USER_ID), \
             patch("cqc_lem.api.main.geocode_city", side_effect=GeocodeError("no match")):
            resp = client.post(self.BASE, json={"session_token": _SESSION, "city": "Nowhere"})
        assert resp.status_code == 422

    def test_invalid_session_401(self, client):
        with patch("cqc_lem.api.main.get_session_user_id", return_value=None):
            resp = client.post(self.BASE, json={"session_token": "bad", "city": "Orlando"})
        assert resp.status_code == 401
