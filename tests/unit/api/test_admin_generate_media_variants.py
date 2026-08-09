"""Unit tests for POST /api/admin/generate-media-variants."""

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


class TestGenerateMediaVariantsEndpoint:
    def test_forbidden_without_secret(self, client):
        with patch("cqc_lem.api.routers.admin.ADMIN_SECRET", "s3cret"):
            r = client.post("/api/admin/generate-media-variants", json={"text": "hi"})
        assert r.status_code == 403

    def test_unprocessable_without_source(self, client):
        with patch("cqc_lem.api.routers.admin.ADMIN_SECRET", "s3cret"):
            r = client.post("/api/admin/generate-media-variants", json={},
                            headers={"x-admin-secret": "s3cret"})
        assert r.status_code == 422

    def test_ok(self, client):
        payload = {"batch_id": "1_abc", "variants": [],
                   "total_estimated_cost_usd": 0.0, "metadata_url": "u"}
        with patch("cqc_lem.api.routers.admin.ADMIN_SECRET", "s3cret"), \
             patch("cqc_lem.app.generate_variants.generate_media_variants", return_value=payload) as gen:
            r = client.post("/api/admin/generate-media-variants",
                            json={"text": "hi", "user_id": 1},
                            headers={"x-admin-secret": "s3cret"})
        assert r.status_code == 200
        assert r.json()["detail"]["batch_id"] == "1_abc"
        assert gen.called
