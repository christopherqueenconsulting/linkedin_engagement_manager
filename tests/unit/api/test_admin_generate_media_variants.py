"""Unit tests for POST /api/admin/generate-media-variants."""

from unittest.mock import patch

import pytest

pytestmark = pytest.mark.unit


class TestGenerateMediaVariantsEndpoint:
    def test_forbidden_without_secret(self, api_client):
        with patch("cqc_lem.api.routers.admin.ADMIN_SECRET", "s3cret"):
            r = api_client.post("/api/admin/generate-media-variants", json={"text": "hi"})
        assert r.status_code == 403

    def test_unprocessable_without_source(self, api_client):
        with patch("cqc_lem.api.routers.admin.ADMIN_SECRET", "s3cret"):
            r = api_client.post("/api/admin/generate-media-variants", json={},
                            headers={"x-admin-secret": "s3cret"})
        assert r.status_code == 422

    def test_ok(self, api_client):
        payload = {"batch_id": "1_abc", "variants": [],
                   "total_estimated_cost_usd": 0.0, "metadata_url": "u"}
        with patch("cqc_lem.api.routers.admin.ADMIN_SECRET", "s3cret"), \
             patch("cqc_lem.app.generate_variants.generate_media_variants", return_value=payload) as gen:
            r = api_client.post("/api/admin/generate-media-variants",
                            json={"text": "hi", "user_id": 1},
                            headers={"x-admin-secret": "s3cret"})
        assert r.status_code == 200
        assert r.json()["detail"]["batch_id"] == "1_abc"
        assert gen.called
