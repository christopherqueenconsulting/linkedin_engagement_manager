"""Coverage tests for api/main.py pure helpers: slide parsing, UTC serialization,
asset path resolution, HTTP range plumbing, and the /api/assets endpoint."""

import pytest
from datetime import datetime
from unittest.mock import MagicMock, patch

from fastapi import HTTPException

pytestmark = pytest.mark.unit

_M = "cqc_lem.api.main"


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


class TestParseSlides:
    def test_none_and_empty(self):
        from cqc_lem.api.main import _parse_slides
        assert _parse_slides(None) is None
        assert _parse_slides("") is None

    def test_list_passthrough(self):
        from cqc_lem.api.main import _parse_slides
        assert _parse_slides(["a", "b"]) == ["a", "b"]

    def test_json_string(self):
        from cqc_lem.api.main import _parse_slides
        assert _parse_slides('["a", "b"]') == ["a", "b"]

    def test_json_non_list_returns_none(self):
        from cqc_lem.api.main import _parse_slides
        assert _parse_slides('{"a": 1}') is None

    def test_invalid_json_returns_none(self):
        from cqc_lem.api.main import _parse_slides
        assert _parse_slides("{broken") is None


class TestUtcIso:
    def test_none(self):
        from cqc_lem.api.main import _utc_iso
        assert _utc_iso(None) is None

    def test_naive_datetime_assumed_utc(self):
        from cqc_lem.api.main import _utc_iso
        assert _utc_iso(datetime(2026, 7, 6, 15, 30)) == "2026-07-06T15:30:00Z"

    def test_aware_datetime_converted(self):
        from cqc_lem.api.main import _utc_iso
        import pytz
        eastern = pytz.timezone("US/Eastern").localize(datetime(2026, 7, 6, 11, 30))
        assert _utc_iso(eastern) == "2026-07-06T15:30:00Z"

    def test_iso_string_with_z(self):
        from cqc_lem.api.main import _utc_iso
        assert _utc_iso("2026-07-06T15:30:00Z") == "2026-07-06T15:30:00Z"

    def test_unparseable_string_returned_as_is(self):
        from cqc_lem.api.main import _utc_iso
        assert _utc_iso("not-a-date") == "not-a-date"


class TestPublicPostUrl:
    def test_http_urls_pass(self):
        from cqc_lem.api.main import _public_post_url
        assert _public_post_url("https://li.com/p/1") == "https://li.com/p/1"
        assert _public_post_url("HTTP://li.com/p/1") == "HTTP://li.com/p/1"

    def test_synthetic_feedpost_key_hidden(self):
        from cqc_lem.api.main import _public_post_url
        assert _public_post_url("feedpost://abc123") is None
        assert _public_post_url(None) is None
        assert _public_post_url(42) is None


class TestFindAssetFile:
    def test_resolves_nested_file(self, tmp_path):
        from cqc_lem.api.main import _find_asset_file
        nested = tmp_path / "videos" / "runwayml"
        nested.mkdir(parents=True)
        f = nested / "clip.mp4"
        f.write_bytes(b"v")
        assert _find_asset_file(str(tmp_path), "videos/runwayml/clip.mp4") == str(f)

    def test_rejects_traversal_components(self, tmp_path):
        from cqc_lem.api.main import _find_asset_file
        (tmp_path / "safe.txt").write_text("x")
        assert _find_asset_file(str(tmp_path), "../safe.txt") is None
        assert _find_asset_file(str(tmp_path), "./safe.txt") is None
        assert _find_asset_file(str(tmp_path), "") is None

    def test_missing_file_returns_none(self, tmp_path):
        from cqc_lem.api.main import _find_asset_file
        assert _find_asset_file(str(tmp_path), "nope.mp4") is None

    def test_directory_target_returns_none(self, tmp_path):
        from cqc_lem.api.main import _find_asset_file
        (tmp_path / "adir").mkdir()
        assert _find_asset_file(str(tmp_path), "adir") is None

    def test_file_used_as_directory_returns_none(self, tmp_path):
        from cqc_lem.api.main import _find_asset_file
        (tmp_path / "afile").write_text("x")
        assert _find_asset_file(str(tmp_path), "afile/child.txt") is None

    def test_unreadable_root_returns_none(self, tmp_path):
        from cqc_lem.api.main import _find_asset_file
        assert _find_asset_file(str(tmp_path / "missing-root"), "a.txt") is None


class TestAssetsEndpoint:
    def test_serves_existing_file(self, client, tmp_path):
        video = tmp_path / "videos" / "clip.mp4"
        video.parent.mkdir(parents=True)
        video.write_bytes(b"\x00videobytes")
        with patch(f"{_M}.assets_dir", str(tmp_path)):
            resp = client.get("/api/assets?file_name=videos/clip.mp4")
        assert resp.status_code == 200
        assert resp.content == b"\x00videobytes"
        assert resp.headers["content-type"].startswith("video/")

    def test_404_for_missing_file(self, client, tmp_path):
        with patch(f"{_M}.assets_dir", str(tmp_path)):
            resp = client.get("/api/assets?file_name=videos/none.mp4")
        assert resp.status_code == 404

    def test_404_for_traversal_attempt(self, client, tmp_path):
        with patch(f"{_M}.assets_dir", str(tmp_path)):
            resp = client.get("/api/assets?file_name=../etc/passwd")
        assert resp.status_code == 404


class TestRangeHelpers:
    def test_send_bytes_range_requests_yields_exact_window(self, tmp_path):
        from cqc_lem.api.main import send_bytes_range_requests
        f = tmp_path / "data.bin"
        f.write_bytes(b"0123456789")
        chunks = b"".join(send_bytes_range_requests(str(f), 2, 5, chunk_size=2))
        assert chunks == b"2345"

    def test_get_range_header_full_form(self):
        from cqc_lem.api.main import _get_range_header
        assert _get_range_header("bytes=0-99", 1000) == (0, 99)

    def test_get_range_header_open_end(self):
        from cqc_lem.api.main import _get_range_header
        assert _get_range_header("bytes=500-", 1000) == (500, 999)

    def test_get_range_header_open_start(self):
        from cqc_lem.api.main import _get_range_header
        assert _get_range_header("bytes=-99", 1000) == (0, 99)

    def test_get_range_header_invalid_number_raises_416(self):
        from cqc_lem.api.main import _get_range_header
        with pytest.raises(HTTPException) as exc:
            _get_range_header("bytes=abc-def", 1000)
        assert exc.value.status_code == 416

    def test_get_range_header_out_of_bounds_raises_416(self):
        from cqc_lem.api.main import _get_range_header
        with pytest.raises(HTTPException) as exc:
            _get_range_header("bytes=900-2000", 1000)
        assert exc.value.status_code == 416

    def test_range_requests_response_partial(self, tmp_path):
        from cqc_lem.api.main import range_requests_response
        f = tmp_path / "video.mp4"
        f.write_bytes(b"0123456789")
        request = MagicMock()
        request.headers.get.return_value = "bytes=2-5"
        resp = range_requests_response(request, str(f), "video/mp4")
        assert resp.status_code == 206
        assert resp.headers["content-range"] == "bytes 2-5/10"
        assert resp.headers["content-length"] == "4"

    def test_range_requests_response_full(self, tmp_path):
        from cqc_lem.api.main import range_requests_response
        f = tmp_path / "video.mp4"
        f.write_bytes(b"0123456789")
        request = MagicMock()
        request.headers.get.return_value = None
        resp = range_requests_response(request, str(f), "video/mp4")
        assert resp.status_code == 200
        assert resp.headers["content-length"] == "10"
        assert resp.headers["accept-ranges"] == "bytes"


class TestAuthInitBypassSessionFailure:
    def test_bypass_session_creation_failure_returns_500(self, client):
        with patch(f"{_M}.get_user_id", return_value=5), \
             patch(f"{_M}.generate_pin", return_value="123456"), \
             patch(f"{_M}.hash_pin", return_value="hashed"), \
             patch(f"{_M}.send_pin_email", return_value=(True, True)), \
             patch(f"{_M}.create_session", return_value=None):
            resp = client.post("/api/auth/email/init", json={"email": "user@example.com"})
        assert resp.status_code == 500


class TestComputeNextPublish:
    def test_bad_timezone_falls_back_to_utc(self):
        from cqc_lem.api.main import _compute_next_publish
        import pytz
        with patch(f"{_M}.get_newsletter_settings", return_value={
                "publish_day": 1, "publish_hour": 9, "cadence": "weekly",
                "last_published_at": None}), \
             patch(f"{_M}.get_user_timezone", return_value="Not/AZone"), \
             patch("cqc_lem.utilities.newsletter.next_publish_datetime",
                   return_value=datetime(2026, 7, 13, 9, 0)) as npd:
            result = _compute_next_publish(1)
        assert result == datetime(2026, 7, 13, 9, 0)
        assert npd.call_args[0][4] is pytz.utc
