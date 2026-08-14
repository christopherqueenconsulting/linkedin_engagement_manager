"""Coverage tests for api/main.py pure helpers: slide parsing, UTC serialization,
asset path resolution, HTTP range plumbing, and the /api/assets endpoint.

The pure helpers are input→output functions, so they are pinned as parametrized
contract tables (issue #1216) in the style of `test_db_coverage_errors.py`; only the
cases that need a live TestClient or a bespoke filesystem stay as plain tests.
"""

from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException

pytestmark = pytest.mark.unit

_M = "cqc_lem.api.main"
_AUTH = "cqc_lem.api.routers.auth"
_USER = "cqc_lem.api.routers.user"


# (case id, raw value, parsed slides)
_PARSE_SLIDES_CASES = [
    ("none", None, None),
    ("empty_string", "", None),
    ("list_passthrough", ["a", "b"], ["a", "b"]),
    ("json_list", '["a", "b"]', ["a", "b"]),
    ("json_object_is_not_slides", '{"a": 1}', None),
    ("invalid_json", "{broken", None),
]


class TestParseSlides:
    @pytest.mark.parametrize("case_id,raw,expected",
                             _PARSE_SLIDES_CASES, ids=[c[0] for c in _PARSE_SLIDES_CASES])
    def test_parses_or_returns_none(self, case_id, raw, expected):
        from cqc_lem.api.main import _parse_slides
        assert _parse_slides(raw) == expected


class TestUtcIso:
    # (case id, value, expected serialization)
    @pytest.mark.parametrize("case_id,value,expected", [
        ("none", None, None),
        ("naive_datetime_assumed_utc", datetime(2026, 7, 6, 15, 30), "2026-07-06T15:30:00Z"),
        ("aware_datetime_converted", "<eastern>", "2026-07-06T15:30:00Z"),
        ("iso_string_with_z", "2026-07-06T15:30:00Z", "2026-07-06T15:30:00Z"),
        ("unparseable_string_returned_as_is", "not-a-date", "not-a-date"),
    ], ids=["none", "naive_datetime_assumed_utc", "aware_datetime_converted",
            "iso_string_with_z", "unparseable_string_returned_as_is"])
    def test_serializes_to_utc(self, case_id, value, expected):
        import pytz

        from cqc_lem.api.main import _utc_iso
        if value == "<eastern>":
            value = pytz.timezone("US/Eastern").localize(datetime(2026, 7, 6, 11, 30))
        assert _utc_iso(value) == expected


class TestPublicPostUrl:
    # A synthetic feedpost:// key is an internal handle, so the SPA must never be handed one.
    @pytest.mark.parametrize("case_id,value,expected", [
        ("https", "https://li.com/p/1", "https://li.com/p/1"),
        ("uppercase_scheme", "HTTP://li.com/p/1", "HTTP://li.com/p/1"),
        ("synthetic_feedpost_key", "feedpost://abc123", None),
        ("none", None, None),
        ("non_string", 42, None),
    ], ids=["https", "uppercase_scheme", "synthetic_feedpost_key", "none", "non_string"])
    def test_only_http_urls_are_public(self, case_id, value, expected):
        from cqc_lem.api.main import _public_post_url
        assert _public_post_url(value) == expected


@pytest.fixture
def asset_root(tmp_path):
    """An assets tree holding one nested file, one loose file, one directory."""
    nested = tmp_path / "videos" / "runwayml"
    nested.mkdir(parents=True)
    (nested / "clip.mp4").write_bytes(b"v")
    (tmp_path / "safe.txt").write_text("x")
    (tmp_path / "adir").mkdir()
    (tmp_path / "afile").write_text("x")
    return tmp_path


class TestFindAssetFile:
    # (case id, requested name, resolved path relative to the root — None means refused)
    @pytest.mark.parametrize("case_id,file_name,resolved", [
        ("nested_file", "videos/runwayml/clip.mp4", "videos/runwayml/clip.mp4"),
        ("parent_traversal", "../safe.txt", None),
        ("dot_component", "./safe.txt", None),
        ("empty_name", "", None),
        ("missing_file", "nope.mp4", None),
        ("directory_target", "adir", None),
        ("file_used_as_directory", "afile/child.txt", None),
    ], ids=["nested_file", "parent_traversal", "dot_component", "empty_name", "missing_file",
            "directory_target", "file_used_as_directory"])
    def test_resolves_only_a_real_file_inside_the_root(self, case_id, file_name, resolved,
                                                       asset_root):
        from cqc_lem.api.main import _find_asset_file
        expected = str(asset_root / resolved) if resolved else None
        assert _find_asset_file(str(asset_root), file_name) == expected

    def test_unreadable_root_returns_none(self, tmp_path):
        from cqc_lem.api.main import _find_asset_file
        assert _find_asset_file(str(tmp_path / "missing-root"), "a.txt") is None


class TestAssetsEndpoint:
    def test_serves_existing_file(self, api_client, tmp_path):
        video = tmp_path / "videos" / "clip.mp4"
        video.parent.mkdir(parents=True)
        video.write_bytes(b"\x00videobytes")
        with patch(f"{_M}.assets_dir", str(tmp_path)):
            resp = api_client.get("/api/assets?file_name=videos/clip.mp4")
        assert resp.status_code == 200
        assert resp.content == b"\x00videobytes"
        assert resp.headers["content-type"].startswith("video/")

    # Both a name that resolves to nothing and one that tries to climb out read as 404 — the
    # traversal attempt must not be distinguishable from a plain miss.
    @pytest.mark.parametrize("file_name", ["videos/none.mp4", "../etc/passwd"],
                             ids=["missing_file", "traversal_attempt"])
    def test_404_for_anything_not_resolved(self, api_client, tmp_path, file_name):
        with patch(f"{_M}.assets_dir", str(tmp_path)):
            resp = api_client.get(f"/api/assets?file_name={file_name}")
        assert resp.status_code == 404


class TestRangeHelpers:
    def test_send_bytes_range_requests_yields_exact_window(self, tmp_path):
        from cqc_lem.api.main import send_bytes_range_requests
        f = tmp_path / "data.bin"
        f.write_bytes(b"0123456789")
        chunks = b"".join(send_bytes_range_requests(str(f), 2, 5, chunk_size=2))
        assert chunks == b"2345"

    # (case id, Range header, resolved (start, end) over a 1000-byte file)
    @pytest.mark.parametrize("case_id,header,expected", [
        ("full_form", "bytes=0-99", (0, 99)),
        ("open_end", "bytes=500-", (500, 999)),
        ("open_start", "bytes=-99", (0, 99)),
    ], ids=["full_form", "open_end", "open_start"])
    def test_get_range_header_resolves_window(self, case_id, header, expected):
        from cqc_lem.api.main import _get_range_header
        assert _get_range_header(header, 1000) == expected

    @pytest.mark.parametrize("case_id,header", [
        ("invalid_number", "bytes=abc-def"),
        ("out_of_bounds", "bytes=900-2000"),
    ], ids=["invalid_number", "out_of_bounds"])
    def test_get_range_header_raises_416(self, case_id, header):
        from cqc_lem.api.main import _get_range_header
        with pytest.raises(HTTPException) as exc:
            _get_range_header(header, 1000)
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
    def test_bypass_session_creation_failure_returns_500(self, api_client):
        with patch(f"{_AUTH}.get_user_id", return_value=5), \
             patch(f"{_AUTH}.generate_pin", return_value="123456"), \
             patch(f"{_AUTH}.hash_pin", return_value="hashed"), \
             patch(f"{_AUTH}.send_pin_email", return_value=(True, True)), \
             patch(f"{_AUTH}.create_session", return_value=None):
            resp = api_client.post("/api/auth/email/init", json={"email": "user@example.com"})
        assert resp.status_code == 500


class TestComputeNextPublish:
    def test_bad_timezone_falls_back_to_utc(self):
        import pytz

        from cqc_lem.api.routers.user import _compute_next_publish
        with patch(f"{_USER}.get_newsletter_settings", return_value={
                "publish_day": 1, "publish_hour": 9, "cadence": "weekly",
                "last_published_at": None}), \
             patch(f"{_USER}.get_user_timezone", return_value="Not/AZone"), \
             patch("cqc_lem.utilities.newsletter.next_publish_datetime",
                   return_value=datetime(2026, 7, 13, 9, 0)) as npd:
            result = _compute_next_publish(1)
        assert result == datetime(2026, 7, 13, 9, 0)
        assert npd.call_args[0][4] is pytz.utc
