"""Post-render clip check (PR #2249 video gauntlet): ffmpeg and lem-vision mocked throughout."""

import json
import os
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from cqc_lem.utilities import video_clip_check as vcc

pytestmark = pytest.mark.unit

_CLEAN = {"objects_appear_or_disappear": False, "limbs_or_faces_distorted": False,
          "text_or_ui_appears": False, "details": ""}
_MUG = {**_CLEAN, "objects_appear_or_disappear": True, "text_or_ui_appears": True,
        "details": "a striped mug appears on the desk in the last frame"}


def _resp(content):
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])


def _fake_ffmpeg(args, **_kwargs):
    with open(args[-1], "wb") as fh:
        fh.write(b"\xff\xd8jpeg")
    return SimpleNamespace(returncode=0)


@pytest.mark.parametrize("value,expected", [(None, True), ("true", True), ("1", True),
                                            ("false", False), ("0", False), (" OFF ", False)])
def test_enabled_is_read_at_call_time(monkeypatch, value, expected):
    if value is None:
        monkeypatch.delenv("VIDEO_CLIP_CHECK_ENABLED", raising=False)
    else:
        monkeypatch.setenv("VIDEO_CLIP_CHECK_ENABLED", value)
    assert vcc.clip_check_enabled() is expected


def test_three_frames_at_the_documented_seconds(tmp_path):
    with patch.object(vcc.shutil, "which", return_value="/usr/bin/ffmpeg"), \
         patch.object(vcc.subprocess, "run", side_effect=_fake_ffmpeg) as run:
        frames = vcc.sample_frames("clip.mp4", str(tmp_path))
    assert len(frames) == 3 and all(os.path.isfile(f) for f in frames)
    assert [c.args[0][3] for c in run.call_args_list] == ["1.0", "2.5", "4.5"]


def test_no_ffmpeg_raises(tmp_path):
    with patch.object(vcc.shutil, "which", return_value=None), pytest.raises(RuntimeError):
        vcc.sample_frames("clip.mp4", str(tmp_path))


def test_a_missing_frame_raises(tmp_path):
    with patch.object(vcc.shutil, "which", return_value="/usr/bin/ffmpeg"), \
         patch.object(vcc.subprocess, "run", return_value=SimpleNamespace(returncode=1)), \
         pytest.raises(RuntimeError, match="no frame at 1.0s"):
        vcc.sample_frames("clip.mp4", str(tmp_path))


def _check(tmp_path, reply, url="/local/clip.mp4"):
    with patch.object(vcc.shutil, "which", return_value="/usr/bin/ffmpeg"), \
         patch.object(vcc.subprocess, "run", side_effect=_fake_ffmpeg), \
         patch("cqc_lem.utilities.ai.client.client.chat.completions.create",
               side_effect=reply if isinstance(reply, Exception) else None,
               return_value=None if isinstance(reply, Exception) else
               _resp(reply if isinstance(reply, str) else json.dumps(reply))) as vis, \
         patch.object(vcc, "log_warning") as warn:
        verdict = vcc.check_clip_url(url, user_id=1, post_id=9)
    return verdict, vis, warn


def test_a_clean_clip_passes_in_one_call_with_all_three_frames(tmp_path):
    verdict, vis, warn = _check(tmp_path, _CLEAN)
    assert verdict.checked and verdict.defects == []
    vis.assert_called_once()
    content = vis.call_args.kwargs["messages"][0]["content"]
    assert len([p for p in content if p["type"] == "image_url"]) == 3
    for _, question, _ in vcc.QUESTIONS:
        assert question in content[0]["text"]
    warn.assert_not_called()


def test_defects_are_named(tmp_path):
    verdict, _, _ = _check(tmp_path, _MUG)
    assert verdict.defects == ["an object or person appeared or disappeared mid-shot",
                               "on-screen text or UI appeared"]
    assert "striped mug" in verdict.details
    clause = vcc.repair_clause(verdict)
    assert "striped mug" in clause and "screens stay dark" in clause


@pytest.mark.parametrize("reply", [RuntimeError("vision 502"), "not json", {"details": "x"}])
def test_any_failure_fails_open_and_warns(tmp_path, reply):
    verdict, _, warn = _check(tmp_path, reply)
    assert verdict.checked is False and verdict.reason
    assert warn.call_args.kwargs["exc"] is not None


def test_a_remote_clip_is_downloaded_then_cleaned_up(tmp_path):
    seen = {}

    def _download(url, directory):
        seen["dir"] = directory
        path = os.path.join(directory, "clip.mp4")
        open(path, "wb").close()
        return path

    with patch("cqc_lem.utilities.utils.save_video_url_to_dir", side_effect=_download):
        verdict, _, _ = _check(tmp_path, _CLEAN, url="https://runway.test/clip.mp4")
    assert verdict.checked
    assert not os.path.exists(seen["dir"]), "the temp dir is removed"


def test_the_receipt_form_is_json_safe():
    verdict = vcc.ClipVerdict(checked=True, defects=["x"], details="d")
    assert json.loads(json.dumps(verdict.to_receipt())) == {
        "checked": True, "defects": ["x"], "details": "d", "reason": ""}
