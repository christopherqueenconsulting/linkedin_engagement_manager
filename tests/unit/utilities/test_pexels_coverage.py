"""Coverage tests for pexels_helper video search/download and photo helpers."""

import pytest
from unittest.mock import MagicMock, patch

pytestmark = pytest.mark.unit

_P = "cqc_lem.utilities.pexels_helper"


class TestSearchVideos:
    def test_no_api_key_returns_empty(self, monkeypatch):
        from cqc_lem.utilities.pexels_helper import search_videos
        monkeypatch.delenv("PEXELS_API_KEY", raising=False)
        assert search_videos("ocean") == []

    def test_queries_pexels_with_auth_header(self, monkeypatch):
        from cqc_lem.utilities.pexels_helper import search_videos
        monkeypatch.setenv("PEXELS_API_KEY", "pk")
        resp = MagicMock()
        resp.json.return_value = {"videos": [{"id": 1}]}
        with patch(f"{_P}.requests.get", return_value=resp) as rget:
            assert search_videos("ocean", per_page=5) == [{"id": 1}]
        kwargs = rget.call_args[1]
        assert kwargs["headers"] == {"Authorization": "pk"}
        assert kwargs["params"]["query"] == "ocean" and kwargs["params"]["per_page"] == 5


class TestGetVideoFileUrl:
    def test_prefers_requested_quality(self):
        from cqc_lem.utilities.pexels_helper import get_video_file_url
        video = {"video_files": [
            {"quality": "hd", "file_type": "video/mp4", "link": "https://hd"},
            {"quality": "sd", "file_type": "video/mp4", "link": "https://sd"}]}
        assert get_video_file_url(video, quality="sd") == "https://sd"

    def test_falls_back_to_any_mp4(self):
        from cqc_lem.utilities.pexels_helper import get_video_file_url
        video = {"video_files": [
            {"quality": "hd", "file_type": "video/mp4", "link": "https://hd"}]}
        assert get_video_file_url(video, quality="sd") == "https://hd"

    def test_no_mp4_returns_none(self):
        from cqc_lem.utilities.pexels_helper import get_video_file_url
        video = {"video_files": [{"quality": "hd", "file_type": "video/webm",
                                  "link": "https://webm"}]}
        assert get_video_file_url(video) is None


class TestDownloadPexelsVideo:
    def test_no_search_results_returns_none(self):
        from cqc_lem.utilities.pexels_helper import download_pexels_video
        with patch(f"{_P}.search_videos", return_value=[]):
            assert download_pexels_video("ocean", "/tmp") is None

    def test_no_mp4_url_returns_none(self):
        from cqc_lem.utilities.pexels_helper import download_pexels_video
        with patch(f"{_P}.search_videos", return_value=[{"id": 1, "video_files": []}]):
            assert download_pexels_video("ocean", "/tmp") is None

    def test_downloads_first_result(self, tmp_path):
        from cqc_lem.utilities.pexels_helper import download_pexels_video
        video = {"id": 42, "video_files": [
            {"quality": "sd", "file_type": "video/mp4", "link": "https://cdn/v.mp4"}]}
        resp = MagicMock()
        resp.iter_content.return_value = [b"chunk1", b"chunk2"]
        with patch(f"{_P}.search_videos", return_value=[video]), \
             patch(f"{_P}.requests.get", return_value=resp):
            path = download_pexels_video("ocean", str(tmp_path))
        assert path == str(tmp_path / "pexels_42.mp4")
        assert (tmp_path / "pexels_42.mp4").read_bytes() == b"chunk1chunk2"


class TestPhotoHelpers:
    def test_get_photos_searches_api(self):
        import cqc_lem.utilities.pexels_helper as ph
        fake_api = MagicMock()
        fake_api.get_entries.return_value = ["photo1", "photo2"]
        with patch.object(ph, "api", fake_api):
            photos = ph.get_photos("sunset", num_of_photos=10)
        assert photos == ["photo1", "photo2"]
        fake_api.search.assert_called_once_with("sunset", page=1, results_per_page=10)

    def test_get_photo_picks_from_results(self):
        import cqc_lem.utilities.pexels_helper as ph
        fake_api = MagicMock()
        fake_api.get_entries.return_value = ["only-photo"]
        with patch.object(ph, "api", fake_api):
            assert ph.get_photo("sunset") == "only-photo"
