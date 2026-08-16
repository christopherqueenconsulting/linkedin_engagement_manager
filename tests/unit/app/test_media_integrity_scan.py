"""The weekly media-integrity report — issue #1377 (P3).

`posts.image_url` / `video_url` were strings nothing ever re-read: 3 of the 4 most recent
media-bearing rows on production pointed at files that were not there, and nothing detected it. The
beat is deliberately a REPORT — these cover that it never repairs, that it never confuses a purged
publish with a defect, and that the reading reaches PostHog, which is the half an audit can read
without production credentials.
"""

from unittest.mock import patch

import pytest

pytestmark = pytest.mark.unit

DB = "cqc_lem.utilities.db"
OBS = "cqc_lem.utilities.observability"


def _url(relative: str) -> str:
    return f"https://api.example.com/api/assets?file_name={relative}"


class TestMediaIntegrityScan:
    def _run(self, tmp_path, rows, **kwargs):
        from cqc_lem.app.run_scheduler import auto_media_integrity_scan
        with patch("cqc_lem.assets_dir", str(tmp_path)), \
             patch(f"{DB}.get_posts_with_media", return_value=rows) as reader, \
             patch(f"{OBS}.track_media_integrity") as track, \
             patch("cqc_lem.utilities.logger.log_error") as error:
            result = auto_media_integrity_scan(**kwargs)
        return result, track, error, reader

    def test_a_dangling_url_before_publication_is_reported_and_escalated(self, tmp_path):
        rows = [{"id": 91, "user_id": 1, "status": "scheduled",
                 "image_url": _url("images/posts/91/img_a1.webp"), "video_url": None}]
        result, track, error, _ = self._run(tmp_path, rows)
        assert "1 dangling" in result
        assert track.call_args.args[0]["dangling"] == 1
        assert track.call_args.args[0]["dangling_posts"] == ["91"]
        assert error.call_count == 1
        assert error.call_args.kwargs["task_name"] == "auto_media_integrity_scan"

    def test_a_published_post_whose_media_was_purged_is_not_a_defect(self, tmp_path):
        # purge_post_assets removes the file the moment LinkedIn re-hosts the media — the correct
        # end state, and the reason this report separates the two counters instead of summing them.
        rows = [{"id": 79, "user_id": 1, "status": "posted",
                 "image_url": _url("images/posts/79/out-0.webp"),
                 "video_url": _url("videos/runwayml/gone.mp4")}]
        result, track, error, _ = self._run(tmp_path, rows)
        assert track.call_args.args[0]["missing_expected"] == 2
        assert track.call_args.args[0]["dangling"] == 0
        assert not error.called
        assert "0 dangling" in result and "2 purged at publish" in result

    def test_a_present_file_and_its_recorded_brief_are_counted(self, tmp_path):
        from cqc_lem.utilities.media_provenance import write_brief_receipt

        class _Brief:
            focal_concept = "the idea"
            prompt = "a brief"
            surface = "post_image"
            style_preset = "post_image"
            ratio = "1:1"

        directory = tmp_path / "images" / "posts" / "84"
        directory.mkdir(parents=True)
        (directory / "img_a1.webp").write_text("bytes")
        url = _url("images/posts/84/img_a1.webp")
        with patch("cqc_lem.assets_dir", str(tmp_path)):
            write_brief_receipt(url, _Brief(), post_id=84)
        rows = [{"id": 84, "user_id": 1, "status": "approved", "image_url": url,
                 "video_url": None}]
        result, track, error, _ = self._run(tmp_path, rows)
        summary = track.call_args.args[0]
        assert summary["present"] == 1 and summary["with_brief"] == 1
        assert not error.called and "1 with a recorded brief" in result

    def test_nothing_to_check_emits_nothing(self, tmp_path):
        result, track, error, _ = self._run(tmp_path, [])
        assert result == "No media rows to check"
        assert not track.called and not error.called

    def test_the_row_walk_is_bounded_and_overridable(self, tmp_path):
        from cqc_lem.app.run_scheduler import MEDIA_INTEGRITY_SCAN_LIMIT
        _result, _track, _error, reader = self._run(tmp_path, [])
        assert reader.call_args.kwargs["limit"] == MEDIA_INTEGRITY_SCAN_LIMIT
        _result, _track, _error, reader = self._run(tmp_path, [], limit=10)
        assert reader.call_args.kwargs["limit"] == 10

    def test_it_never_removes_a_file_or_touches_a_row(self, tmp_path):
        directory = tmp_path / "images" / "posts" / "84"
        directory.mkdir(parents=True)
        stored = directory / "img_a1.webp"
        stored.write_text("bytes")
        rows = [{"id": 84, "user_id": 1, "status": "posted",
                 "image_url": _url("images/posts/84/img_a1.webp"),
                 "video_url": _url("videos/runwayml/gone.mp4")}]
        with patch(f"{DB}.update_db_post_image_url") as writer:
            self._run(tmp_path, rows)
        assert stored.exists() and not writer.called
