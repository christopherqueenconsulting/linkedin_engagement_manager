"""Unit tests for POST /api/admin/user/location (admin login-location override)."""

from unittest.mock import patch

import pytest

pytestmark = pytest.mark.unit


@pytest.fixture(scope="module")
def client():
    patches = [
        patch("cqc_lem.utilities.observability.track_api_call"),
        patch("cqc_lem.app.run_automation.automate_invites_to_company_page_for_user"),
        patch("cqc_lem.app.run_automation.automate_reply_commenting"),
        patch("cqc_lem.app.run_content_plan.auto_create_weekly_content"),
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


_BASE = "/api/admin/user/location"
_GEO = {"latitude": 28.5384, "longitude": -81.3789, "city": "Orlando",
        "country": "US", "timezone": "America/New_York", "locale": "en-US"}
_BODY = {"user_id": 42, "city": "Orlando", "state": "Florida", "country": "US"}


class TestAdminSetUserLocation:
    def test_forbidden_without_secret(self, client):
        with patch("cqc_lem.api.main.ADMIN_SECRET", "s3cret"):
            resp = client.post(_BASE, json=_BODY)
        assert resp.status_code == 403

    def test_forbidden_wrong_secret(self, client):
        with patch("cqc_lem.api.main.ADMIN_SECRET", "s3cret"):
            resp = client.post(_BASE, json=_BODY, headers={"x-admin-secret": "nope"})
        assert resp.status_code == 403

    def test_404_when_user_missing(self, client):
        with patch("cqc_lem.api.main.ADMIN_SECRET", "s3cret"), \
             patch("cqc_lem.api.main.get_user_geo", return_value=None):
            resp = client.post(_BASE, json=_BODY, headers={"x-admin-secret": "s3cret"})
        assert resp.status_code == 404

    def test_422_on_geocode_miss(self, client):
        from cqc_lem.utilities.geocoding import GeocodeError
        with patch("cqc_lem.api.main.ADMIN_SECRET", "s3cret"), \
             patch("cqc_lem.api.main.get_user_geo", return_value={"latitude": 1.0}), \
             patch("cqc_lem.api.main.geocode_city", side_effect=GeocodeError("no match")):
            resp = client.post(_BASE, json=_BODY, headers={"x-admin-secret": "s3cret"})
        assert resp.status_code == 422

    def test_200_happy_path_saves_manual(self, client):
        with patch("cqc_lem.api.main.ADMIN_SECRET", "s3cret"), \
             patch("cqc_lem.api.main.get_user_geo", return_value={"latitude": 1.0}), \
             patch("cqc_lem.api.main.geocode_city", return_value=_GEO), \
             patch("cqc_lem.api.main.update_user_location", return_value=True) as save:
            resp = client.post(_BASE, json=_BODY, headers={"x-admin-secret": "s3cret"})
        assert resp.status_code == 200
        assert resp.json()["detail"]["city"] == "Orlando"
        assert save.call_args.kwargs["source"] == "manual"
        assert save.call_args.args[0] == 42  # user_id
