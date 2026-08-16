"""Stored media URLs vs. what is behind them — issue #1377.

Two findings, one module. The integrity half has to answer a question that only makes sense with the
post's STATUS in hand: a `posted` row whose file is gone is `purge_post_assets` doing its job, and
the same reading on a row that has not published is the defect P3 named. Conflate the two and the
report is noise on day one.

The receipt half is the P4 record: `focal_concept` was thrown away at render time, so R6 ("the
render depicts the brief's stated idea") could never be scored after the fact. These cover that it
is keyed by the STORED url, that nothing fabricates one, and that a broken file reads as absent
rather than as a brief.
"""

import json
import os
from dataclasses import dataclass
from unittest.mock import patch

import pytest

from cqc_lem.utilities import media_provenance as mp

pytestmark = pytest.mark.unit

_MP = "cqc_lem.utilities.media_provenance"


@dataclass
class _Brief:
    """Stand-in for `ImageBrief` — the module reads it by attribute, never by import."""

    focal_concept: str = "a steering wheel, not a brake pedal"
    prompt: str = "a 40-word photograph brief"
    surface: str = "post_image"
    style_preset: str = "post_image"
    ratio: str = "1:1"


def _url(relative: str) -> str:
    return f"https://lem.example.com/api/assets?file_name={relative}"


class TestAssetRelativePath:
    def test_reads_our_own_asset_url(self):
        assert mp.asset_relative_path(_url("images/posts/84/img_a1.webp")) == \
            "images/posts/84/img_a1.webp"

    def test_reads_a_generated_video_path_the_post_helpers_refuse(self):
        # `post_video_relative_path` only resolves compose-time previews; a report that skipped the
        # generated path would answer "nothing dangling" for the column P3 was filed about.
        assert mp.asset_relative_path(_url("videos/runwayml/clip.mp4")) == "videos/runwayml/clip.mp4"

    def test_a_foreign_url_is_not_ours(self):
        assert mp.asset_relative_path("https://cdn.example.com/img.png") is None

    def test_traversal_never_resolves(self):
        assert mp.asset_relative_path(_url("images/../../etc/passwd")) is None

    def test_empty_input(self):
        assert mp.asset_relative_path(None) is None
        assert mp.asset_relative_path(_url("")) is None


class TestStoredAssetPath:
    def test_answers_a_path_for_a_file_that_is_gone(self, tmp_path):
        # Existence-agnostic on purpose — this is what a dangling row and a surviving receipt both
        # need, and it is the one thing `post_image_abs_path` will not do.
        with patch("cqc_lem.assets_dir", str(tmp_path)):
            path = mp.stored_asset_path(_url("images/posts/79/out-0.webp"))
        assert path == os.path.join(str(tmp_path), "images", "posts", "79", "out-0.webp")

    def test_a_path_escaping_the_volume_answers_nothing(self, tmp_path):
        assets = tmp_path / "assets"
        (assets / "images").mkdir(parents=True)
        outside = tmp_path / "outside.png"
        outside.write_text("x")
        os.symlink(str(outside), str(assets / "images" / "link.png"))
        with patch("cqc_lem.assets_dir", str(assets)):
            assert mp.stored_asset_path(_url("images/link.png")) is None


class TestBriefReceiptRoundTrip:
    def test_written_beside_the_stored_file_and_read_back_by_url(self, tmp_path):
        url = _url("images/posts/84/img_a1.webp")
        with patch("cqc_lem.assets_dir", str(tmp_path)):
            path = mp.write_brief_receipt(url, _Brief(), post_id=84, user_id=1,
                                          gate_verdict="accepted")
            assert path == os.path.join(str(tmp_path), "images", "posts", "84",
                                        "img_a1" + mp.BRIEF_RECEIPT_SUFFIX)
            recovered = mp.read_brief_receipt(url)
        assert recovered["focal_concept"] == "a steering wheel, not a brake pedal"
        assert recovered["gate_verdict"] == "accepted"
        assert recovered["style_preset"] == "post_image"
        assert recovered["post_id"] == 84 and recovered["user_id"] == 1
        assert recovered["media"] == "images/posts/84/img_a1.webp"

    def test_survives_the_file_it_describes(self, tmp_path):
        # The whole point: the audit reads content that has ALREADY shipped, by which time
        # purge_post_assets has removed the render.
        url = _url("videos/runwayml/clip.mp4")
        with patch("cqc_lem.assets_dir", str(tmp_path)):
            mp.write_brief_receipt(url, _Brief(surface="video"), post_id=83)
            assert mp.stored_asset_path(url) and not os.path.isfile(mp.stored_asset_path(url))
            assert mp.read_brief_receipt(url)["surface"] == "video"

    def test_no_brief_records_nothing(self, tmp_path):
        with patch("cqc_lem.assets_dir", str(tmp_path)):
            assert mp.write_brief_receipt(_url("videos/runwayml/stock.mp4"), None) is None
            assert mp.write_brief_receipt(_url("videos/runwayml/stock.mp4"),
                                          _Brief(focal_concept="  ")) is None
            assert mp.read_brief_receipt(_url("videos/runwayml/stock.mp4")) is None

    def test_a_url_that_is_not_ours_records_nothing(self, tmp_path):
        with patch("cqc_lem.assets_dir", str(tmp_path)):
            assert mp.write_brief_receipt("https://cdn.example.com/x.mp4", _Brief()) is None

    def test_an_unwritable_directory_warns_and_answers_none(self, tmp_path):
        url = _url("images/posts/84/img_a1.webp")
        with patch("cqc_lem.assets_dir", str(tmp_path)), \
                patch(f"{_MP}.os.makedirs", side_effect=OSError("read-only")), \
                patch(f"{_MP}.log_warning") as warn:
            assert mp.write_brief_receipt(url, _Brief(), post_id=84) is None
        assert warn.called

    def test_a_receipt_that_will_not_parse_is_no_receipt(self, tmp_path):
        url = _url("images/posts/84/img_a1.webp")
        with patch("cqc_lem.assets_dir", str(tmp_path)):
            path = mp.brief_receipt_path(url)
            os.makedirs(os.path.dirname(path))
            open(path, "w", encoding="utf-8").write("{ truncated")
            assert mp.read_brief_receipt(url) is None

    def test_a_payload_with_no_focal_concept_is_no_receipt(self, tmp_path):
        url = _url("images/posts/84/img_a1.webp")
        with patch("cqc_lem.assets_dir", str(tmp_path)):
            path = mp.brief_receipt_path(url)
            os.makedirs(os.path.dirname(path))
            open(path, "w", encoding="utf-8").write(json.dumps({"prompt": "words"}))
            assert mp.read_brief_receipt(url) is None


class TestIsBriefReceipt:
    def test_recognises_its_own_sidecar(self):
        assert mp.is_brief_receipt("/a/img_a1" + mp.BRIEF_RECEIPT_SUFFIX)
        assert not mp.is_brief_receipt("/a/img_a1.webp")
        assert not mp.is_brief_receipt(None)


class TestScanPostMedia:
    def _volume(self, tmp_path):
        directory = tmp_path / "images" / "posts" / "84"
        directory.mkdir(parents=True)
        (directory / "img_a1.webp").write_text("bytes")
        return tmp_path

    def test_a_present_file_grades_present(self, tmp_path):
        with patch("cqc_lem.assets_dir", str(self._volume(tmp_path))):
            rows = mp.scan_post_media([{"id": 84, "user_id": 1, "status": "approved",
                                        "image_url": _url("images/posts/84/img_a1.webp")}])
        assert len(rows) == 1
        assert rows[0].state == mp.MEDIA_PRESENT and not rows[0].dangling

    def test_a_missing_file_on_a_posted_row_is_the_purge_doing_its_job(self, tmp_path):
        with patch("cqc_lem.assets_dir", str(tmp_path)):
            rows = mp.scan_post_media([{"id": 79, "user_id": 1, "status": "posted",
                                        "image_url": _url("images/posts/79/out-0.webp")}])
        assert rows[0].state == mp.MEDIA_MISSING and rows[0].expected
        assert not rows[0].dangling

    def test_the_purged_status_is_the_enum_not_a_literal(self):
        # This one comparison decides defect vs no-op, so it must not drift from the column's
        # vocabulary — `posts.status` is a MySQL ENUM and `PostStatus` is its Python mirror.
        from cqc_lem.platform.db.enums import PostStatus

        assert mp._PURGED_AT_PUBLISH_STATUSES == (PostStatus.POSTED.value,)

    def test_a_missing_file_before_publication_is_the_defect(self, tmp_path):
        with patch("cqc_lem.assets_dir", str(tmp_path)):
            rows = mp.scan_post_media([{"id": 91, "user_id": 1, "status": "scheduled",
                                        "video_url": _url("videos/runwayml/gone.mp4")}])
        assert rows[0].state == mp.MEDIA_MISSING and not rows[0].expected
        assert rows[0].dangling and rows[0].column == "video_url"

    def test_a_url_that_is_not_ours_is_never_counted_as_missing(self, tmp_path):
        with patch("cqc_lem.assets_dir", str(tmp_path)):
            rows = mp.scan_post_media([{"id": 92, "user_id": 1, "status": "pending",
                                        "image_url": "https://cdn.example.com/hand-edited.png"}])
        assert rows[0].state == mp.MEDIA_UNRESOLVABLE and not rows[0].dangling

    def test_both_columns_grade_separately_and_empties_are_skipped(self, tmp_path):
        with patch("cqc_lem.assets_dir", str(self._volume(tmp_path))):
            rows = mp.scan_post_media([
                {"id": 84, "user_id": 1, "status": "posted",
                 "image_url": _url("images/posts/84/img_a1.webp"),
                 "video_url": _url("videos/runwayml/clip.mp4")},
                {"id": 85, "user_id": 1, "status": "posted", "image_url": "", "video_url": None},
            ])
        assert [(row.column, row.state) for row in rows] == [
            ("image_url", mp.MEDIA_PRESENT), ("video_url", mp.MEDIA_MISSING)]

    def test_a_recorded_brief_is_reported_per_row(self, tmp_path):
        volume = self._volume(tmp_path)
        url = _url("images/posts/84/img_a1.webp")
        with patch("cqc_lem.assets_dir", str(volume)):
            mp.write_brief_receipt(url, _Brief(), post_id=84)
            rows = mp.scan_post_media([{"id": 84, "user_id": 1, "status": "posted",
                                        "image_url": url}])
        assert rows[0].has_brief

    def test_no_rows_no_readings(self):
        assert mp.scan_post_media(None) == []


class TestIntegritySummary:
    def test_a_defect_is_never_summed_with_an_expected_purge(self, tmp_path):
        with patch("cqc_lem.assets_dir", str(tmp_path)):
            rows = mp.scan_post_media([
                {"id": 79, "user_id": 1, "status": "posted",
                 "image_url": _url("images/posts/79/out-0.webp")},
                {"id": 91, "user_id": 1, "status": "scheduled",
                 "video_url": _url("videos/runwayml/gone.mp4")},
                {"id": 92, "user_id": 1, "status": "pending",
                 "image_url": "https://cdn.example.com/x.png"},
            ])
        summary = mp.integrity_summary(rows)
        assert summary["checked"] == 3
        assert summary["dangling"] == 1 and summary["dangling_posts"] == ["91"]
        assert summary["missing_expected"] == 1
        assert summary["unresolvable"] == 1
        assert summary["present"] == 0 and summary["with_brief"] == 0

    def test_the_post_id_sample_is_bounded_but_the_count_is_not(self, tmp_path):
        with patch("cqc_lem.assets_dir", str(tmp_path)):
            rows = mp.scan_post_media([
                {"id": post_id, "user_id": 1, "status": "approved",
                 "image_url": _url(f"images/posts/{post_id}/img.webp")}
                for post_id in range(1, 8)])
        summary = mp.integrity_summary(rows, dangling_sample=3)
        assert summary["dangling"] == 7
        assert summary["dangling_posts"] == ["1", "2", "3"]

    def test_nothing_graded_reads_as_zero_everywhere(self):
        assert mp.integrity_summary([])["checked"] == 0
        assert mp.integrity_summary(None)["dangling_posts"] == []
