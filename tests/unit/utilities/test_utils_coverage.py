"""Coverage tests for cqc_lem.utilities.utils helpers (debug wrapper, prompts, file/AWS
helpers, asset purging).

The pure input→output helpers (the confirm prompt, extension parsing) are parametrized
tables (issue #1216); the download, AWS and purge paths stay plain because each asserts a
different side effect.
"""

from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.unit

_U = "cqc_lem.utilities.utils"


class TestDebugFunction:
    def test_calls_wrapped_function_and_returns_result(self, capsys):
        import cqc_lem.utilities.utils as utils
        with patch.object(utils, "DEBUG_LEVEL", 3):
            @utils.debug_function
            def add(a, b):
                return a + b
            assert add(2, 3) == 5
        out = capsys.readouterr().out
        assert "Before calling function add" in out
        assert "After calling function add" in out

    def test_level_two_skips_execution(self, capsys):
        import cqc_lem.utilities.utils as utils
        with patch.object(utils, "DEBUG_LEVEL", 2):
            calls = []

            @utils.debug_function
            def side_effecting():
                calls.append(1)
                return "ran"
            assert side_effecting() is None
        assert calls == []
        assert "Calling function side_effecting" in capsys.readouterr().out

    def test_level_zero_runs_silently(self, capsys):
        import cqc_lem.utilities.utils as utils
        with patch.object(utils, "DEBUG_LEVEL", 0):
            @utils.debug_function
            def f():
                return 7
            assert f() == 7
        assert capsys.readouterr().out == ""


class TestAreYouSatisfied:
    # (case id, what the operator typed — a list means one entry per re-prompt, expected answer)
    @pytest.mark.parametrize("case_id,typed,expected", [
        ("yes", ["1"], True),
        ("no", ["0"], False),
        ("empty_defaults_to_yes", [""], True),
        ("invalid_reprompts", ["9", "1"], True),
    ], ids=["yes", "no", "empty_defaults_to_yes", "invalid_reprompts"])
    def test_reads_the_confirmation(self, case_id, typed, expected):
        from cqc_lem.utilities.utils import are_you_satisfied
        with patch("builtins.input", side_effect=typed):
            assert are_you_satisfied() is expected


class TestCreateFolderIfNotExists:
    def test_creates_missing_folder(self, tmp_path):
        from cqc_lem.utilities.utils import create_folder_if_not_exists
        target = tmp_path / "a" / "b"
        create_folder_if_not_exists(str(target))
        assert target.is_dir()

    def test_noop_when_folder_exists(self, tmp_path):
        from cqc_lem.utilities.utils import create_folder_if_not_exists
        create_folder_if_not_exists(str(tmp_path))  # must not raise
        assert tmp_path.is_dir()


class TestSaveVideoUrlToDir:
    @staticmethod
    def _response(chunks, raise_on=None):
        """A streamed `requests` response usable as a context manager."""
        resp = MagicMock()
        resp.__enter__.return_value = resp

        def _iter(chunk_size=None):
            for chunk in chunks:
                yield chunk
            if raise_on is not None:
                raise raise_on

        resp.iter_content.side_effect = _iter
        return resp

    def test_downloads_and_keeps_original_filename(self, tmp_path):
        from cqc_lem.utilities.utils import save_video_url_to_dir
        resp = self._response([b"\x00video", b"-bytes"])
        with patch(f"{_U}.requests.get", return_value=resp) as rget:
            path = save_video_url_to_dir("https://cdn.example.com/media/clip.mp4?sig=abc",
                                         str(tmp_path))
        assert rget.call_args.args[0] == "https://cdn.example.com/media/clip.mp4?sig=abc"
        resp.raise_for_status.assert_called_once()
        assert path == str(tmp_path / "clip.mp4")
        assert (tmp_path / "clip.mp4").read_bytes() == b"\x00video-bytes"

    def test_the_download_is_bounded_and_streamed(self, tmp_path):
        """A render host that accepts the connection then stalls must not park a worker forever."""
        from cqc_lem.utilities.utils import DOWNLOAD_TIMEOUT, save_video_url_to_dir
        resp = self._response([b"x"])
        with patch(f"{_U}.requests.get", return_value=resp) as rget:
            save_video_url_to_dir("https://cdn.example.com/clip.mp4", str(tmp_path))
        assert rget.call_args.kwargs["timeout"] == DOWNLOAD_TIMEOUT
        assert rget.call_args.kwargs["stream"] is True

    # The documented invariant: never leave a truncated file later stages read as media. Both
    # ways a download can die must raise AND leave the directory untouched.
    @pytest.mark.parametrize("case_id,exc", [
        ("interrupted_mid_stream", OSError("connection reset")),
        ("non_2xx_response", RuntimeError("404")),
    ], ids=["interrupted_mid_stream", "non_2xx_response"])
    def test_a_failed_download_leaves_no_file_at_all(self, tmp_path, case_id, exc):
        from cqc_lem.utilities.utils import save_video_url_to_dir
        if case_id == "interrupted_mid_stream":
            resp = self._response([b"partial"], raise_on=exc)
        else:
            resp = self._response([])
            resp.raise_for_status.side_effect = exc
        with patch(f"{_U}.requests.get", return_value=resp):
            with pytest.raises(type(exc)):
                save_video_url_to_dir("https://cdn.example.com/clip.mp4", str(tmp_path))
        assert list(tmp_path.iterdir()) == []


class TestFileExtension:
    # (case id, path, kwargs, extension) — always lower-cased, so a .MP4 upload and a .mp4 one
    # take the same downstream branch.
    @pytest.mark.parametrize("case_id,path,kwargs,expected", [
        ("lowercases", "/a/b/Video.MP4", {}, ".mp4"),
        ("removes_leading_dot", "/a/b/c.PNG", {"remove_leading_dot": True}, "png"),
        ("no_extension", "/a/b/Makefile", {}, ""),
    ], ids=["lowercases", "removes_leading_dot", "no_extension"])
    def test_reads_the_extension(self, case_id, path, kwargs, expected):
        from cqc_lem.utilities.utils import get_file_extension_from_filepath
        assert get_file_extension_from_filepath(path, **kwargs) == expected


class TestAwsHelpers:
    def test_get_aws_client_builds_client_from_session(self):
        from cqc_lem.utilities.utils import get_aws_client
        session = MagicMock()
        with patch(f"{_U}.boto3.session.Session", return_value=session):
            client = get_aws_client("s3", "us-east-1")
        session.client.assert_called_once_with(service_name="s3", region_name="us-east-1")
        assert client is session.client.return_value

    def test_get_cloudwatch_client_uses_default_region(self):
        from cqc_lem.utilities.utils import get_cloudwatch_client
        session = MagicMock()
        with patch(f"{_U}.boto3.session.Session", return_value=session):
            get_cloudwatch_client()
        session.client.assert_called_once_with(service_name="cloudwatch",
                                               region_name="us-east-1")

    def test_get_aws_ssm_secret_parses_json(self):
        from cqc_lem.utilities.utils import get_aws_ssm_secret
        session = MagicMock()
        session.client.return_value.get_secret_value.return_value = {
            "SecretString": '{"host": "db", "port": 3306}'}
        with patch(f"{_U}.boto3.session.Session", return_value=session):
            secret = get_aws_ssm_secret("my-secret", "us-east-1")
        assert secret == {"host": "db", "port": 3306}
        session.client.return_value.get_secret_value.assert_called_once_with(
            SecretId="my-secret")

    def test_get_aws_ssm_secret_reraises_client_error(self):
        from cqc_lem.utilities.utils import get_aws_ssm_secret
        session = MagicMock()
        session.client.return_value.get_secret_value.side_effect = RuntimeError("denied")
        with patch(f"{_U}.boto3.session.Session", return_value=session):
            with pytest.raises(RuntimeError, match="denied"):
                get_aws_ssm_secret("my-secret", "us-east-1")

    def test_get_aws_device_farm_url(self):
        from cqc_lem.utilities.utils import get_aws_device_farm_url
        session = MagicMock()
        session.client.return_value.create_test_grid_url.return_value = {
            "url": "https://grid.example.com"}
        with patch(f"{_U}.boto3.session.Session", return_value=session):
            url = get_aws_device_farm_url("123456789012", "proj-arn", expiresInSeconds=60)
        assert url == "https://grid.example.com"
        kwargs = session.client.return_value.create_test_grid_url.call_args[1]
        assert kwargs["expiresInSeconds"] == 60
        assert "testgrid-project:proj-arn" in kwargs["projectArn"]


class TestPurgePostAssets:
    def _setup_assets(self, tmp_path):
        video_dir = tmp_path / "videos" / "runwayml"
        video_dir.mkdir(parents=True)
        video = video_dir / "clip.mp4"
        video.write_bytes(b"v")
        carousel_dir = tmp_path / "images" / "carousel" / "9"
        carousel_dir.mkdir(parents=True)
        (carousel_dir / "slide1.png").write_bytes(b"i")
        return video, carousel_dir

    def test_removes_video_and_carousel_dir(self, tmp_path):
        from cqc_lem.utilities.utils import purge_post_assets
        video, carousel_dir = self._setup_assets(tmp_path)
        url = "https://api.example.com/api/assets?file_name=videos/runwayml/clip.mp4"
        with patch("cqc_lem.assets_dir", str(tmp_path)):
            removed = purge_post_assets(9, video_url=url)
        assert not video.exists()
        assert not carousel_dir.exists()
        assert str(video) in removed and str(carousel_dir) in removed

    def test_path_traversal_in_file_name_is_ignored(self, tmp_path):
        from cqc_lem.utilities.utils import purge_post_assets
        outside = tmp_path.parent / "outside.mp4"
        outside.write_bytes(b"x")
        url = "https://api.example.com/api/assets?file_name=../outside.mp4"
        with patch("cqc_lem.assets_dir", str(tmp_path / "assets-nonexistent")):
            removed = purge_post_assets(1, video_url=url)
        assert outside.exists()
        assert removed == []

    # Nothing to remove is never an error: a purge runs on posts whose assets were already
    # gone, and on rows whose video_url was never an assets URL at all.
    @pytest.mark.parametrize("case_id,post_id,video_url", [
        ("missing_asset", 123,
         "https://api.example.com/api/assets?file_name=videos/none.mp4"),
        ("unparseable_video_url", 9, "https://x.com/no-query"),
    ], ids=["missing_asset", "unparseable_video_url"])
    def test_nothing_to_remove_is_tolerated(self, tmp_path, case_id, post_id, video_url):
        from cqc_lem.utilities.utils import purge_post_assets
        with patch("cqc_lem.assets_dir", str(tmp_path)):
            assert purge_post_assets(post_id, video_url=video_url) == []

    def test_no_video_url_only_purges_carousel(self, tmp_path):
        from cqc_lem.utilities.utils import purge_post_assets
        _, carousel_dir = self._setup_assets(tmp_path)
        with patch("cqc_lem.assets_dir", str(tmp_path)):
            removed = purge_post_assets(9)
        assert removed == [str(carousel_dir)]
        assert not carousel_dir.exists()


class TestGetPostTime:
    def test_prefers_user_recommendation_for_weekday(self):
        from datetime import date, time

        from cqc_lem.utilities.utils import get_post_time
        # 2026-07-08 is a Wednesday (weekday 2); default is 16:00
        recs = [{"weekday_num": 2, "hour": 9}]
        with patch("cqc_lem.utilities.db.get_post_engagement_rows", return_value=[]), \
             patch("cqc_lem.utilities.db.get_user_timezone", return_value="America/New_York"), \
             patch("cqc_lem.utilities.post_stats.recommend_post_times", return_value=recs):
            assert get_post_time(date(2026, 7, 8), user_id=1) == time(9, 0)

    def test_falls_back_to_default_without_user(self):
        from datetime import date, time

        from cqc_lem.utilities.utils import get_best_posting_time, get_post_time
        d = date(2026, 7, 8)
        assert get_post_time(d) == get_best_posting_time(d) == time(16, 0)

    def test_falls_back_to_default_on_error(self):
        from datetime import date, time

        from cqc_lem.utilities.utils import get_post_time
        with patch("cqc_lem.utilities.db.get_post_engagement_rows",
                   side_effect=RuntimeError("db down")):
            assert get_post_time(date(2026, 7, 8), user_id=1) == time(16, 0)
