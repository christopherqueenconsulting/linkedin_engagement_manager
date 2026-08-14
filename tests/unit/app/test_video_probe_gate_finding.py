"""The video probe's verdict as review-queue UX (issue #1402).

A rejected video file is never stored, so by the time the quality gates run the post merely looks
assetless. These tests pin the one thing that keeps the reason readable: the probe records WHY it
rejected the file on `posts.gate_reason`, the gate pass carries that note next to the missing-asset
hold it explains, and a passing probe clears it again.
"""

from unittest.mock import patch

import pytest

import cqc_lem.app.run_content_plan as rcp
from cqc_lem.utilities.quality_gates import (
    GATE_MALFORMED_ASSET,
    GATE_MISSING_ASSET,
    authenticity_finding,
    malformed_asset_finding,
)

_RCP = "cqc_lem.app.run_content_plan"


def _malformed(reason: str = "zero-byte file", demoted: bool = False) -> dict:
    return malformed_asset_finding("video", reason, demoted=demoted)


class TestRecordVideoProbeFinding:
    def test_a_failed_probe_persists_the_reason_as_an_advisory_note(self):
        with patch(f"{_RCP}.get_post_gate_reason", return_value=[]), \
             patch(f"{_RCP}.update_db_post_gate_reason") as store, \
             patch.object(rcp, "VIDEO_PROBE_ENABLED", False):
            rcp._record_video_probe_finding(7, False, "zero-byte file", user_id=1)
        findings = store.call_args[0][1]
        assert [f["gate"] for f in findings] == [GATE_MALFORMED_ASSET]
        assert "zero-byte file" in findings[0]["explanation"]
        assert findings[0]["details"] == ["zero-byte file"]
        # Advisory while the probe is fail-open: missing_asset is already holding this post, so the
        # note explains the hold instead of adding a second one.
        assert findings[0]["demoted"] is False

    def test_the_note_demotes_only_when_the_probe_flag_makes_it_a_hard_failure(self):
        with patch(f"{_RCP}.get_post_gate_reason", return_value=[]), \
             patch(f"{_RCP}.update_db_post_gate_reason") as store, \
             patch.object(rcp, "VIDEO_PROBE_ENABLED", True):
            rcp._record_video_probe_finding(7, False, "missing MP4 ftyp signature")
        assert store.call_args[0][1][0]["demoted"] is True

    def test_other_gates_survive_and_the_note_never_duplicates(self):
        prior = [authenticity_finding(41, 60), _malformed("zero-byte file")]
        with patch(f"{_RCP}.get_post_gate_reason", return_value=prior), \
             patch(f"{_RCP}.update_db_post_gate_reason") as store, \
             patch.object(rcp, "VIDEO_PROBE_ENABLED", False):
            rcp._record_video_probe_finding(7, False, "invalid MP4 ftyp box size")
        findings = store.call_args[0][1]
        assert [f["gate"] for f in findings] == ["authenticity", GATE_MALFORMED_ASSET]
        assert findings[-1]["details"] == ["invalid MP4 ftyp box size"]

    def test_a_passing_probe_clears_a_stale_note(self):
        with patch(f"{_RCP}.get_post_gate_reason",
                   return_value=[authenticity_finding(41, 60), _malformed()]), \
             patch(f"{_RCP}.update_db_post_gate_reason") as store:
            rcp._record_video_probe_finding(7, True, "")
        assert [f["gate"] for f in store.call_args[0][1]] == ["authenticity"]

    def test_a_passing_probe_with_nothing_to_clear_never_writes(self):
        with patch(f"{_RCP}.get_post_gate_reason", return_value=[authenticity_finding(41, 60)]), \
             patch(f"{_RCP}.update_db_post_gate_reason") as store:
            rcp._record_video_probe_finding(7, True, "")
        store.assert_not_called()

    def test_an_unreadable_reason_never_costs_the_post(self):
        with patch(f"{_RCP}.get_post_gate_reason", side_effect=RuntimeError("db down")), \
             patch(f"{_RCP}.update_db_post_gate_reason") as store, \
             patch(f"{_RCP}.log_warning") as warn:
            rcp._record_video_probe_finding(7, False, "zero-byte file")
        store.assert_not_called()
        warn.assert_called_once()


class TestAcceptProbedVideoRecordsIt:
    def test_a_rejection_records_the_probe_reason_before_returning_false(self):
        with patch(f"{_RCP}._probe_video_file", return_value=(False, "zero-byte file")), \
             patch(f"{_RCP}.track_video_asset_probe"), \
             patch(f"{_RCP}._record_video_probe_finding") as record, \
             patch.object(rcp, "VIDEO_PROBE_ENABLED", False):
            assert rcp._accept_probed_video(7, "/tmp/clip.mp4", "https://runway.test/x.mp4",
                                            user_id=1, task_name="auto_create_weekly_content") is False
        record.assert_called_once()
        assert record.call_args[0][:3] == (7, False, "zero-byte file")

    def test_the_reason_is_recorded_even_when_the_flag_turns_it_into_a_hard_failure(self):
        with patch(f"{_RCP}._probe_video_file", return_value=(False, "zero-byte file")), \
             patch(f"{_RCP}.track_video_asset_probe"), \
             patch(f"{_RCP}._record_video_probe_finding") as record, \
             patch.object(rcp, "VIDEO_PROBE_ENABLED", True):
            with pytest.raises(RuntimeError):
                rcp._accept_probed_video(7, "/tmp/clip.mp4", "https://runway.test/x.mp4")
        record.assert_called_once()

    def test_a_passing_probe_still_reports_its_verdict_so_a_healed_post_clears(self):
        with patch(f"{_RCP}._probe_video_file", return_value=(True, "")), \
             patch(f"{_RCP}.track_video_asset_probe"), \
             patch(f"{_RCP}._record_video_probe_finding") as record:
            assert rcp._accept_probed_video(7, "/tmp/clip.mp4", "https://runway.test/x.mp4") is True
        assert record.call_args[0][:2] == (7, True)


class TestGatePassCarriesTheNote:
    """`evaluate_post_gates` cannot re-derive the reason — the rejected file was never stored."""

    def _gates(self, video_url, recorded):
        with patch(f"{_RCP}._post_missing_required_asset", return_value=video_url is None), \
             patch(f"{_RCP}.get_post_gate_reason", return_value=recorded):
            return rcp.evaluate_post_gates(7, "", "video", video_url)

    def test_the_recorded_reason_rides_alongside_the_missing_asset_hold(self):
        findings = self._gates(None, [_malformed("zero-byte file")])
        assert [f["gate"] for f in findings] == [GATE_MISSING_ASSET, GATE_MALFORMED_ASSET]
        assert findings[0]["demoted"] is True and findings[1]["demoted"] is False
        assert "zero-byte file" in findings[1]["explanation"]

    def test_a_post_that_now_has_media_stops_showing_the_stale_reason(self):
        findings = self._gates("https://api.test/api/assets?file_name=videos/runwayml/clip.mp4",
                               [_malformed("zero-byte file")])
        assert findings == []

    def test_a_post_that_never_failed_a_probe_gets_only_the_missing_asset_hold(self):
        assert [f["gate"] for f in self._gates(None, [])] == [GATE_MISSING_ASSET]
