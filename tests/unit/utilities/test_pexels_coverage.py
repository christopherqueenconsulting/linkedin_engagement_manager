"""Coverage tests for pexels_helper video search/download and photo helpers.

The file-picking and give-up contracts are parametrized tables (issue #1216); the
request-shape and write-to-disk cases stay plain because each asserts something different.
"""

from unittest.mock import MagicMock, patch

import pytest

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


_HD_MP4 = {"quality": "hd", "file_type": "video/mp4", "link": "https://hd"}
_SD_MP4 = {"quality": "sd", "file_type": "video/mp4", "link": "https://sd"}
_HD_WEBM = {"quality": "hd", "file_type": "video/webm", "link": "https://webm"}


class TestGetVideoFileUrl:
    # (case id, the files Pexels offered, requested quality, chosen link)
    @pytest.mark.parametrize("case_id,files,quality,expected", [
        ("prefers_requested_quality", [_HD_MP4, _SD_MP4], "sd", "https://sd"),
        ("falls_back_to_any_mp4", [_HD_MP4], "sd", "https://hd"),
        # Only MP4 is playable downstream, so a webm-only video is no video at all.
        ("no_mp4_returns_none", [_HD_WEBM], None, None),
    ], ids=["prefers_requested_quality", "falls_back_to_any_mp4", "no_mp4_returns_none"])
    def test_picks_the_playable_file(self, case_id, files, quality, expected):
        from cqc_lem.utilities.pexels_helper import get_video_file_url
        video = {"video_files": files}
        kwargs = {"quality": quality} if quality else {}
        assert get_video_file_url(video, **kwargs) == expected


class TestDownloadPexelsVideo:
    # Nothing to download and nothing playable are the same answer: None, no file written.
    @pytest.mark.parametrize("case_id,results", [
        ("no_search_results", []),
        ("no_mp4_url", [{"id": 1, "video_files": []}]),
    ], ids=["no_search_results", "no_mp4_url"])
    def test_returns_none_without_a_usable_video(self, case_id, results):
        from cqc_lem.utilities.pexels_helper import download_pexels_video
        with patch(f"{_P}.search_videos", return_value=results):
            assert download_pexels_video("ocean", "/tmp") is None

    def test_downloads_first_result(self, tmp_path):
        from cqc_lem.utilities.pexels_helper import download_pexels_video
        video = {"id": 42, "video_files": [_SD_MP4 | {"link": "https://cdn/v.mp4"}]}
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
        with patch.object(ph, "_get_api", return_value=fake_api):
            photos = ph.get_photos("sunset", num_of_photos=10)
        assert photos == ["photo1", "photo2"]
        fake_api.search.assert_called_once_with("sunset", page=1, results_per_page=10)

    def test_get_photo_picks_from_results(self):
        import cqc_lem.utilities.pexels_helper as ph
        fake_api = MagicMock()
        fake_api.get_entries.return_value = ["only-photo"]
        with patch.object(ph, "_get_api", return_value=fake_api):
            assert ph.get_photo("sunset") == "only-photo"

    def test_get_photos_without_a_key_returns_empty(self, monkeypatch):
        """No key configured is the degrade path, not an exception."""
        import cqc_lem.utilities.pexels_helper as ph
        monkeypatch.delenv("PEXELS_API_KEY", raising=False)
        monkeypatch.setattr(ph, "_api", None)
        assert ph.get_photos("sunset") == []

    def test_get_photo_raises_indexerror_when_pool_is_empty(self, monkeypatch):
        """`get_pexels_image_path` catches IndexError to reach its default path — keep it reachable."""
        import cqc_lem.utilities.pexels_helper as ph
        monkeypatch.delenv("PEXELS_API_KEY", raising=False)
        monkeypatch.setattr(ph, "_api", None)
        with pytest.raises(IndexError):
            ph.get_photo("sunset")


class TestGetApi:
    def test_importing_without_a_key_does_not_raise(self, monkeypatch):
        """Import-time `API(os.environ['PEXELS_API_KEY'])` used to make an unset key a KeyError."""
        import importlib

        import cqc_lem.utilities.pexels_helper as ph
        monkeypatch.delenv("PEXELS_API_KEY", raising=False)
        reloaded = importlib.reload(ph)
        assert reloaded._get_api() is None

    def test_builds_the_client_once_and_caches_it(self, monkeypatch):
        import cqc_lem.utilities.pexels_helper as ph
        monkeypatch.setenv("PEXELS_API_KEY", "pk")
        monkeypatch.setattr(ph, "_api", None)
        with patch(f"{_P}.API", return_value="client") as api_cls:
            assert ph._get_api() == "client"
            assert ph._get_api() == "client"
        api_cls.assert_called_once_with("pk")
