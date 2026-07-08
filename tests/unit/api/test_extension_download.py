"""Unit tests for GET /api/extension/linkedin-connect.zip."""

import io
import zipfile

import pytest
from unittest.mock import patch

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


class TestDownloadExtension:
    def test_returns_zip_with_extension_files(self, client):
        resp = client.get("/api/extension/linkedin-connect.zip")
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "application/zip"
        assert "lem-linkedin-connect.zip" in resp.headers["content-disposition"]

        zf = zipfile.ZipFile(io.BytesIO(resp.content))
        assert zf.testzip() is None  # no corrupt members
        names = set(zf.namelist())
        assert "manifest.json" in names
        assert "popup.html" in names
        assert "popup.js" in names

    def test_manifest_is_valid_mv3(self, client):
        import json
        resp = client.get("/api/extension/linkedin-connect.zip")
        zf = zipfile.ZipFile(io.BytesIO(resp.content))
        manifest = json.loads(zf.read("manifest.json"))
        assert manifest["manifest_version"] == 3
        assert "cookies" in manifest["permissions"]
