"""Integration tests for Pexels Video search and download.

Tests that make live API calls are skipped when PEXELS_API_KEY is absent.
Structural/fallback tests run without a key.
"""
import os
from unittest.mock import patch

import pytest


@pytest.mark.integration
@pytest.mark.slow
class TestPexelsVideoSearch:
    def test_search_videos_returns_results(self):
        """Pexels video search returns at least one result for a common query."""
        if not os.environ.get("PEXELS_API_KEY"):
            pytest.skip("PEXELS_API_KEY not set")

        import requests

        from cqc_lem.utilities.pexels_helper import search_videos

        try:
            videos = search_videos("technology business", per_page=5)
        except requests.exceptions.HTTPError as e:
            # A present-but-invalid/expired key (401/403) is functionally the same
            # as "no key" for this live-call test — skip rather than fail CI.
            status = getattr(e.response, "status_code", None)
            if status in (401, 403):
                pytest.skip(f"PEXELS_API_KEY unauthorized ({status})")
            raise

        assert isinstance(videos, list)
        assert len(videos) > 0, "Expected at least one video result from Pexels"
        assert "video_files" in videos[0], "Each video should have a video_files list"

    def test_search_videos_returns_empty_list_without_key(self):
        """search_videos returns [] when PEXELS_API_KEY is not configured."""
        with patch.dict(os.environ, {"PEXELS_API_KEY": ""}):
            from cqc_lem.utilities.pexels_helper import search_videos
            result = search_videos("anything")
            assert result == []

    def test_get_video_file_url_returns_sd_mp4(self):
        """get_video_file_url extracts the sd-quality mp4 link from a video dict."""
        from cqc_lem.utilities.pexels_helper import get_video_file_url

        video = {
            "id": 123,
            "video_files": [
                {"quality": "hd", "file_type": "video/mp4", "link": "https://example.com/hd.mp4"},
                {"quality": "sd", "file_type": "video/mp4", "link": "https://example.com/sd.mp4"},
            ]
        }
        assert get_video_file_url(video, quality="sd") == "https://example.com/sd.mp4"

    def test_get_video_file_url_falls_back_to_any_mp4(self):
        """get_video_file_url returns the first mp4 when the requested quality is unavailable."""
        from cqc_lem.utilities.pexels_helper import get_video_file_url

        video = {
            "video_files": [
                {"quality": "hd", "file_type": "video/mp4", "link": "https://example.com/hd.mp4"},
            ]
        }
        assert get_video_file_url(video, quality="sd") == "https://example.com/hd.mp4"

    def test_get_video_file_url_returns_none_when_no_mp4(self):
        """get_video_file_url returns None when no mp4 files exist."""
        from cqc_lem.utilities.pexels_helper import get_video_file_url

        assert get_video_file_url({"video_files": []}) is None
        assert get_video_file_url({}) is None

    def test_download_pexels_video_saves_file(self, tmp_path):
        """download_pexels_video saves an mp4 file to disk when the API returns a result."""
        if not os.environ.get("PEXELS_API_KEY"):
            pytest.skip("PEXELS_API_KEY not set")

        import requests

        from cqc_lem.utilities.pexels_helper import download_pexels_video

        dest = str(tmp_path)
        try:
            path = download_pexels_video("technology", dest)
        except requests.exceptions.HTTPError as e:
            # A present-but-invalid/expired key (401/403) — e.g. Dependabot runs that cannot read
            # the Actions-store PEXELS_API_KEY — is functionally "no key" for this live-call test.
            status = getattr(e.response, "status_code", None)
            if status in (401, 403):
                pytest.skip(f"PEXELS_API_KEY unauthorized ({status})")
            raise

        if path is None:
            pytest.skip("Pexels returned no video results for 'technology'")

        assert os.path.exists(path), "Expected a file to be downloaded"
        assert os.path.getsize(path) > 0, "Downloaded file should be non-empty"
        assert path.endswith(".mp4"), "Expected an mp4 file"

    def test_download_pexels_video_returns_none_on_no_results(self, tmp_path):
        """download_pexels_video returns None when search_videos returns empty."""
        with patch("cqc_lem.utilities.pexels_helper.search_videos", return_value=[]):
            from cqc_lem.utilities.pexels_helper import download_pexels_video
            result = download_pexels_video("anything", str(tmp_path))
            assert result is None
