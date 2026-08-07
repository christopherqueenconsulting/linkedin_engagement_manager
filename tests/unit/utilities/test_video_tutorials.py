"""Unit tests for the automated video-tutorial pipeline (issue #505).

Every external boundary is mocked — Selenium capture, TTS, ffmpeg/ffprobe and the YouTube upload —
so the whole production runs end to end in-process. The guardrails get the most attention: this
pipeline publishes to a public channel, so "fails closed" has to be pinned, not assumed.
"""

import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from cqc_lem.utilities.marketing import video_tutorials as vt, youtube_auth as ya

pytestmark = pytest.mark.unit

_MOD = "cqc_lem.utilities.marketing.video_tutorials"


@pytest.fixture(autouse=True)
def _isolated_assets(tmp_path, monkeypatch):
    monkeypatch.setattr(vt, "assets_dir", str(tmp_path))
    monkeypatch.setattr(vt, "TUTORIAL_SPA_BASE_URL", "https://lem.test")
    monkeypatch.setattr(vt, "API_URL_FINAL", "https://lem.test")
    monkeypatch.setattr(vt, "TUTORIAL_DEMO_SESSION_TOKEN", "")
    # The producer is gated by the `tutorial-videos-enabled` FLAG now (issue #651); with the
    # unit lane pinned to env-only flags, setting the env var IS setting the flag.
    monkeypatch.setenv("TUTORIAL_VIDEOS_ENABLED", "true")
    monkeypatch.setattr(vt, "TUTORIAL_THUMBNAIL_ENABLED", False)
    # YouTube credentials live in youtube_auth now (issue #742) — publishing is unconfigured here
    # unless a test says otherwise, and neither the DB credential store nor Redis is touched.
    monkeypatch.setattr(ya, "YOUTUBE_CLIENT_ID", "")
    monkeypatch.setattr(ya, "YOUTUBE_CLIENT_SECRET", "")
    monkeypatch.setattr(ya, "YOUTUBE_REFRESH_TOKEN", "")
    monkeypatch.setattr(ya, "get_app_credential", lambda name: None)
    monkeypatch.setattr(ya, "set_app_credential", lambda *a, **kw: True)
    monkeypatch.setattr(ya, "_redis", lambda: None)
    yield


def _flow(steps=2, requires_auth=False, key="sample"):
    return vt.TutorialFlow(
        key=key,
        title="Sample feature tour",
        feature="The sample feature does exactly one grounded thing.",
        steps=tuple(vt.TutorialStep(caption=f"Step {i}", path=f"/s{i}", wait_for="main",
                                    seconds=3.0)
                    for i in range(steps)),
        requires_auth=requires_auth,
    )


def _configure_youtube(monkeypatch):
    monkeypatch.setattr(ya, "YOUTUBE_CLIENT_ID", "cid")
    monkeypatch.setattr(ya, "YOUTUBE_CLIENT_SECRET", "secret")
    monkeypatch.setattr(ya, "YOUTUBE_REFRESH_TOKEN", "refresh")


def _token_response(status_code=200, payload=None):
    response = MagicMock(headers={})
    response.status_code = status_code
    response.json.return_value = payload if payload is not None else {"access_token": "at"}
    return response


def _driver(screenshot=True):
    def _save(path):
        if not screenshot:
            return False
        with open(path, "wb") as f:
            f.write(b"png")
        return True

    driver = MagicMock()
    driver.save_screenshot.side_effect = _save
    return driver


def _capture(markers=("Dashboard", "Review queue"), frames=("/tmp/a.png", "/tmp/b.png")):
    return {"frames": list(frames), "markers": list(markers),
            "fingerprint": vt.ui_fingerprint(list(markers))}


_ORDINALS = ("first", "second", "third", "fourth", "fifth")


def _script_json(segments=2, **overrides):
    # Deliberately digit-free: a number the captured screens do not contain would (correctly)
    # trip the ungrounded-claim guardrail.
    payload = {
        "intro": "Here is the sample feature.",
        "segments": [f"You open the {_ORDINALS[i]} screen and read what is there."
                     for i in range(segments)],
        "outro": "Open your own account and try it.",
        "youtube_title": "Sample feature tour",
        "youtube_description": "A short tour of the sample feature.",
    }
    payload.update(overrides)
    return json.dumps(payload)


def _llm_response(content):
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])


class TestFlowRegistry:
    def test_every_declared_flow_is_capturable(self):
        assert vt.TUTORIAL_FLOWS
        for key, flow in vt.TUTORIAL_FLOWS.items():
            assert flow.key == key
            assert flow.steps, f"{key} has no steps"
            for step in flow.steps:
                assert step.caption and step.path.startswith("/")
                assert step.wait_for, f"{key} step '{step.caption}' has no verifiable UI anchor"


class TestManifest:
    def test_round_trips_and_tolerates_a_corrupt_file(self):
        assert vt.load_manifest() == {"videos": {}}
        vt.save_manifest({"videos": {"a": {"produced_at": "2026-07-01T00:00:00+00:00"}}})
        assert "a" in vt.load_manifest()["videos"]
        with open(vt.manifest_path(), "w", encoding="utf-8") as f:
            f.write("{not json")
        assert vt.load_manifest() == {"videos": {}}

    def test_asset_url_is_a_public_api_assets_link(self):
        assert vt.asset_url("videos/tutorials/x.mp4") == \
            "https://lem.test/api/assets?file_name=videos/tutorials/x.mp4"


class TestFingerprintAndCadence:
    def test_fingerprint_changes_only_when_the_ui_text_changes(self):
        base = vt.ui_fingerprint(["Dashboard", "Review"])
        assert base == vt.ui_fingerprint(["Dashboard ", " Review"])
        assert base != vt.ui_fingerprint(["Dashboard", "Reviewed"])

    def test_next_flow_prefers_an_uncovered_public_feature(self):
        flow = vt.next_flow({"videos": {}})
        assert flow is not None and not flow.requires_auth

    def test_next_flow_skips_auth_flows_without_a_demo_session(self):
        covered = {"videos": {k: {"produced_at": "2999-01-01T00:00:00+00:00",
                                  "fingerprint": "x"}
                              for k, f in vt.TUTORIAL_FLOWS.items() if not f.requires_auth}}
        assert vt.next_flow(covered) is None

    def test_next_flow_returns_an_auth_flow_once_a_demo_session_exists(self, monkeypatch):
        monkeypatch.setattr(vt, "TUTORIAL_DEMO_SESSION_TOKEN", "tok")
        covered = {"videos": {k: {"produced_at": "2999-01-01T00:00:00+00:00"}
                              for k, f in vt.TUTORIAL_FLOWS.items() if not f.requires_auth}}
        flow = vt.next_flow(covered)
        assert flow is not None and flow.requires_auth

    def test_next_flow_refilms_the_stalest_covered_flow(self, monkeypatch):
        monkeypatch.setattr(vt, "TUTORIAL_REFRESH_DAYS", 1)
        videos = {k: {"produced_at": "2020-01-01T00:00:00+00:00"}
                  for k, f in vt.TUTORIAL_FLOWS.items() if not f.requires_auth}
        videos[sorted(videos)[0]]["produced_at"] = "2019-01-01T00:00:00+00:00"
        assert vt.next_flow({"videos": videos}).key == sorted(videos)[0]

    def test_is_current_requires_both_a_matching_ui_and_a_fresh_render(self, monkeypatch):
        record = {"fingerprint": "abc", "produced_at": "2999-01-01T00:00:00+00:00"}
        assert vt.is_current(record, "abc") is True
        assert vt.is_current(record, "changed") is False
        assert vt.is_current(None, "abc") is False
        monkeypatch.setattr(vt, "TUTORIAL_REFRESH_DAYS", 1)
        assert vt.is_current({"fingerprint": "abc", "produced_at": "2020-01-01T00:00:00+00:00"},
                             "abc") is False
        assert vt.is_current({"fingerprint": "abc", "produced_at": "not-a-date"}, "abc") is False


class TestCapture:
    def test_captures_one_frame_per_step_and_fingerprints_the_ui(self, monkeypatch):
        driver = _driver()
        element = MagicMock()
        element.text = "Dashboard heading"
        with patch("cqc_lem.utilities.selenium_util.find_first", return_value=element), \
             patch("cqc_lem.utilities.selenium_util.click_first") as click:
            result = vt.capture_flow(_flow(steps=2), driver=driver)
        assert len(result["frames"]) == 2
        assert all(os.path.exists(p) for p in result["frames"])
        assert result["fingerprint"] == vt.ui_fingerprint(["Dashboard heading"] * 2)
        assert driver.get.call_args_list[0][0][0] == "https://lem.test/s0"
        click.assert_not_called()
        driver.quit.assert_not_called()  # caller-owned drivers are left open

    def test_clicks_a_step_that_declares_one(self):
        flow = vt.TutorialFlow(key="clicky", title="t", feature="f", steps=(
            vt.TutorialStep(caption="c", path="/x", wait_for="main", click="button.next"),))
        element = MagicMock()
        element.text = "x"
        with patch("cqc_lem.utilities.selenium_util.find_first", return_value=element), \
             patch("cqc_lem.utilities.selenium_util.click_first") as click:
            vt.capture_flow(flow, driver=_driver())
        assert click.call_count == 1

    def test_missing_anchor_fails_closed(self):
        with patch("cqc_lem.utilities.selenium_util.find_first", return_value=None):
            with pytest.raises(vt.TutorialCaptureError, match="anchor"):
                vt.capture_flow(_flow(steps=1), driver=_driver())

    def test_failed_screenshot_fails_closed(self):
        driver = MagicMock()
        driver.save_screenshot.return_value = False
        element = MagicMock()
        element.text = "x"
        with patch("cqc_lem.utilities.selenium_util.find_first", return_value=element):
            with pytest.raises(vt.TutorialCaptureError, match="screenshot"):
                vt.capture_flow(_flow(steps=1), driver=driver)

    def test_auth_flow_without_a_demo_session_never_opens_a_browser(self):
        with patch("cqc_lem.utilities.selenium_util.get_docker_driver") as get_driver:
            with pytest.raises(vt.TutorialCaptureError, match="TUTORIAL_DEMO_SESSION_TOKEN"):
                vt.capture_flow(_flow(steps=1, requires_auth=True))
        get_driver.assert_not_called()

    def test_auth_flow_seeds_the_demo_session_before_filming(self, monkeypatch):
        monkeypatch.setattr(vt, "TUTORIAL_DEMO_SESSION_TOKEN", "tok-123")
        monkeypatch.setattr(vt, "TUTORIAL_DEMO_EMAIL", "demo@lem.test")
        driver = _driver()
        element = MagicMock()
        element.text = "x"
        with patch("cqc_lem.utilities.selenium_util.find_first", return_value=element):
            vt.capture_flow(_flow(steps=1, requires_auth=True), driver=driver)
        script_args = driver.execute_script.call_args[0]
        assert "lem_session" in script_args[0]
        assert script_args[1] == "tok-123" and script_args[2] == "demo@lem.test"

    def test_owned_driver_is_always_quit(self):
        driver = _driver()
        with patch("cqc_lem.utilities.selenium_util.get_docker_driver", return_value=driver), \
             patch("cqc_lem.utilities.selenium_util.find_first", return_value=None):
            with pytest.raises(vt.TutorialCaptureError):
                vt.capture_flow(_flow(steps=1))
        driver.quit.assert_called_once()


class TestScriptGeneration:
    def test_builds_intro_step_and_outro_segments(self):
        flow = _flow(steps=2)
        with patch("cqc_lem.utilities.ai.ai_helper._call_llm",
                   return_value=_llm_response(_script_json(2))) as call:
            script = vt.generate_script(flow, _capture())
        assert [s["caption"] for s in script["segments"]] == \
            [flow.title, "Step 0", "Step 1", "Next step"]
        assert script["title"] == "Sample feature tour"
        assert call.call_args.kwargs["model"] == "lem-medium"
        assert call.call_args.kwargs["_track_feature"] == "marketing"

    def test_prompt_is_grounded_in_the_captured_screen_text(self):
        with patch("cqc_lem.utilities.ai.ai_helper._call_llm",
                   return_value=_llm_response(_script_json(2))) as call:
            vt.generate_script(_flow(steps=2), _capture(markers=["Review queue is empty"]))
        prompt = call.call_args.kwargs["messages"][1]["content"]
        assert "Review queue is empty" in prompt

    def test_narration_is_tts_safe_plain_text(self):
        raw = _script_json(1, intro="It is “ready” — truly — to go.")
        with patch("cqc_lem.utilities.ai.ai_helper._call_llm", return_value=_llm_response(raw)):
            script = vt.generate_script(_flow(steps=1), _capture())
        intro = script["segments"][0]["narration"]
        assert "“" not in intro and "—" not in intro
        assert intro.startswith("It's")

    def test_unparseable_reply_raises_before_any_spend(self):
        with patch("cqc_lem.utilities.ai.ai_helper._call_llm",
                   return_value=_llm_response("sorry, no script today")):
            with pytest.raises(vt.TutorialGuardrailError, match="parse"):
                vt.generate_script(_flow(steps=1), _capture())

    def test_too_few_segments_raises(self):
        with patch("cqc_lem.utilities.ai.ai_helper._call_llm",
                   return_value=_llm_response(_script_json(1))):
            with pytest.raises(vt.TutorialGuardrailError):
                vt.generate_script(_flow(steps=3), _capture())

    def test_fabricated_number_raises(self):
        raw = _script_json(1, outro="It saves you 14 hours every single week.")
        with patch("cqc_lem.utilities.ai.ai_helper._call_llm", return_value=_llm_response(raw)):
            with pytest.raises(vt.TutorialGuardrailError, match="ungrounded"):
                vt.generate_script(_flow(steps=1), _capture())

    def test_number_that_appears_on_the_captured_screen_is_allowed(self):
        raw = _script_json(1, outro="The queue holds 12 drafts.")
        with patch("cqc_lem.utilities.ai.ai_helper._call_llm", return_value=_llm_response(raw)):
            script = vt.generate_script(_flow(steps=1), _capture(markers=["12 drafts waiting"]))
        assert "12 drafts" in script["narration"]

    def test_profanity_raises(self):
        raw = _script_json(1, outro="This damn thing just works.")
        with patch("cqc_lem.utilities.ai.ai_helper._call_llm", return_value=_llm_response(raw)):
            with pytest.raises(vt.TutorialGuardrailError, match="disallowed language"):
                vt.generate_script(_flow(steps=1), _capture())

    def test_over_long_narration_raises(self, monkeypatch):
        monkeypatch.setattr(vt, "TUTORIAL_MAX_NARRATION_CHARS", 40)
        with patch("cqc_lem.utilities.ai.ai_helper._call_llm",
                   return_value=_llm_response(_script_json(2))):
            with pytest.raises(vt.TutorialGuardrailError, match="over the"):
                vt.generate_script(_flow(steps=2), _capture())

    def test_empty_narration_raises(self):
        with pytest.raises(vt.TutorialGuardrailError, match="no narration"):
            vt.check_narration("   ", "anything")

    def test_ungrounded_claims_ignores_formatting_differences(self):
        assert vt.ungrounded_claims("costs $ 29 a month", "the plan is $29") == []
        assert vt.ungrounded_claims("saves 40%", "no numbers here") == ["40%"]


class TestVoiceOver:
    def test_openai_provider_writes_audio_and_bills_the_characters(self, tmp_path, monkeypatch):
        monkeypatch.setattr(vt, "TUTORIAL_TTS_PROVIDER", "openai")
        out = str(tmp_path / "seg.mp3")
        response = SimpleNamespace(content=b"mp3-bytes")
        with patch("cqc_lem.utilities.ai.client.client.audio.speech.create",
                   return_value=response) as create, \
             patch(f"{_MOD}.track_media_cost") as track:
            vt.synthesize_segment("hello there", out)
        written = Path(out).read_bytes()
        assert written == b"mp3-bytes"
        assert create.call_args.kwargs["model"] == "lem-tts"
        kind, provider, usd = track.call_args[0]
        assert (kind, provider) == ("audio", "openai")
        assert usd == pytest.approx(0.015 * len("hello there") / 1000)
        assert track.call_args.kwargs["feature"] == "marketing"

    def test_openai_write_to_file_wrapper_is_used_when_present(self, tmp_path, monkeypatch):
        monkeypatch.setattr(vt, "TUTORIAL_TTS_PROVIDER", "openai")
        out = str(tmp_path / "seg.mp3")
        response = MagicMock()
        response.write_to_file.side_effect = lambda p: Path(p).write_bytes(b"x")
        with patch("cqc_lem.utilities.ai.client.client.audio.speech.create",
                   return_value=response), patch(f"{_MOD}.track_media_cost"):
            vt.synthesize_segment("hi", out)
        response.write_to_file.assert_called_once_with(out)

    def test_elevenlabs_provider_posts_to_the_premium_api(self, tmp_path, monkeypatch):
        monkeypatch.setattr(vt, "TUTORIAL_TTS_PROVIDER", "elevenlabs")
        monkeypatch.setattr(vt, "ELEVENLABS_API_KEY", "k")
        monkeypatch.setattr(vt, "ELEVENLABS_VOICE_ID", "v1")
        out = str(tmp_path / "seg.mp3")
        response = MagicMock(content=b"eleven")
        with patch("requests.post", return_value=response) as post, \
             patch(f"{_MOD}.track_media_cost") as track:
            vt.synthesize_segment("hello", out)
        assert "v1" in post.call_args[0][0]
        written = Path(out).read_bytes()
        assert written == b"eleven"
        assert track.call_args[0][1] == "elevenlabs"

    def test_elevenlabs_without_credentials_fails_closed(self, tmp_path, monkeypatch):
        monkeypatch.setattr(vt, "TUTORIAL_TTS_PROVIDER", "elevenlabs")
        monkeypatch.setattr(vt, "ELEVENLABS_API_KEY", "")
        with pytest.raises(vt.TutorialRenderError, match="elevenlabs"):
            vt.synthesize_segment("hello", str(tmp_path / "x.mp3"))

    def test_cost_rates_differ_per_provider(self):
        assert vt.tts_cost_usd(1000, "openai") == pytest.approx(0.015)
        assert vt.tts_cost_usd(1000, "elevenlabs") == pytest.approx(0.15)
        assert vt.tts_cost_usd(0) == 0.0


def _fake_ffmpeg(duration: str = "4.0"):
    """Stand-in for ffmpeg/ffprobe that also CREATES its output file, so callers that read the
    render back (the YouTube upload) work against a real path.
    """
    def _run(cmd, capture_output=True, text=True):
        if os.path.basename(cmd[0]) == "ffprobe":
            return SimpleNamespace(returncode=0, stdout=duration, stderr="")
        with open(cmd[-1], "wb") as f:
            f.write(b"\x00mp4")
        return SimpleNamespace(returncode=0, stdout="", stderr="")
    return _run


class TestRender:
    def test_missing_ffmpeg_fails_closed(self, monkeypatch):
        monkeypatch.setattr(vt.shutil, "which", lambda name: None)
        with pytest.raises(vt.TutorialRenderError, match="not installed"):
            vt.audio_duration("/tmp/a.mp3")

    def test_ffmpeg_failure_surfaces_stderr(self, monkeypatch):
        monkeypatch.setattr(vt.shutil, "which", lambda name: f"/usr/bin/{name}")
        with patch("subprocess.run", return_value=SimpleNamespace(returncode=1, stdout="",
                                                                 stderr="boom")):
            with pytest.raises(vt.TutorialRenderError, match="boom"):
                vt.audio_duration("/tmp/a.mp3")

    def test_audio_duration_tolerates_unparseable_output(self, monkeypatch):
        monkeypatch.setattr(vt.shutil, "which", lambda name: f"/usr/bin/{name}")
        with patch("subprocess.run", return_value=SimpleNamespace(returncode=0, stdout="n/a",
                                                                 stderr="")):
            assert vt.audio_duration("/tmp/a.mp3") == 0.0

    def test_brand_card_renders_a_real_landscape_png(self, tmp_path):
        from PIL import Image
        path = vt.brand_card("Sample feature", str(tmp_path / "card.png"), subtitle="lem.test")
        with Image.open(path) as image:
            assert image.size == (vt.FRAME_WIDTH, vt.FRAME_HEIGHT)

    def test_captions_use_the_real_segment_durations(self, tmp_path):
        segments = [{"narration": "First line.", "duration": 2.5},
                    {"narration": "Second line.", "duration": 1.25}]
        srt = vt.write_captions(segments, str(tmp_path / "c.srt"))
        body = Path(srt).read_text(encoding="utf-8")
        assert "00:00:00,000 --> 00:00:02,500" in body
        assert "00:00:02,500 --> 00:00:03,750" in body
        assert "Second line." in body

    def test_assemble_video_holds_each_frame_for_its_narration(self, tmp_path, monkeypatch):
        monkeypatch.setattr(vt.shutil, "which", lambda name: f"/usr/bin/{name}")
        frame = str(tmp_path / "f.png")
        Path(frame).write_bytes(b"png")
        segments = [{"frame": frame, "duration": 3.0}, {"frame": frame, "duration": 5.5}]
        out = str(tmp_path / "out.mp4")
        with patch("subprocess.run", side_effect=_fake_ffmpeg()) as run:
            vt.assemble_video(segments, str(tmp_path / "a.mp3"), out)
        concat = Path(f"{out}.frames.txt").read_text(encoding="utf-8")
        assert "duration 3.000" in concat and "duration 5.500" in concat
        assert concat.count("file '") == 3  # last frame repeated for the concat demuxer
        cmd = run.call_args[0][0]
        assert "libx264" in cmd and cmd[-1] == out
        assert "subtitles=" not in " ".join(cmd)

    def test_captions_are_burned_in_only_when_enabled(self, tmp_path, monkeypatch):
        monkeypatch.setattr(vt.shutil, "which", lambda name: f"/usr/bin/{name}")
        monkeypatch.setattr(vt, "TUTORIAL_BURN_CAPTIONS", True)
        frame = str(tmp_path / "f.png")
        Path(frame).write_bytes(b"png")
        with patch("subprocess.run", side_effect=_fake_ffmpeg()) as run:
            vt.assemble_video([{"frame": frame, "duration": 1.0}], str(tmp_path / "a.mp3"),
                              str(tmp_path / "o.mp4"), str(tmp_path / "c.srt"))
        assert "subtitles=" in " ".join(run.call_args[0][0])

    def test_vertical_clip_is_nine_by_sixteen(self, tmp_path, monkeypatch):
        monkeypatch.setattr(vt.shutil, "which", lambda name: f"/usr/bin/{name}")
        with patch("subprocess.run", side_effect=_fake_ffmpeg()) as run:
            vt.render_vertical_clip(str(tmp_path / "in.mp4"), str(tmp_path / "v.mp4"))
        assert f"scale={vt.VERTICAL_WIDTH}:{vt.VERTICAL_HEIGHT}" in " ".join(run.call_args[0][0])

    def test_thumbnail_is_off_by_default_and_never_raises(self, tmp_path, monkeypatch):
        assert vt.generate_thumbnail(_flow(), str(tmp_path)) is None
        monkeypatch.setattr(vt, "TUTORIAL_THUMBNAIL_ENABLED", True)
        with patch("cqc_lem.utilities.ai.image_gen.render_image_from_prompt",
                   side_effect=RuntimeError("no image")):
            assert vt.generate_thumbnail(_flow(), str(tmp_path)) is None

    def test_thumbnail_is_saved_when_enabled(self, tmp_path, monkeypatch):
        monkeypatch.setattr(vt, "TUTORIAL_THUMBNAIL_ENABLED", True)
        rendered = tmp_path / "src" / "a.png"
        rendered.parent.mkdir()
        rendered.write_bytes(b"png")
        out_dir = tmp_path / "out"
        with patch("cqc_lem.utilities.ai.image_gen.render_image_from_prompt",
                   return_value=str(rendered)) as render:
            assert vt.generate_thumbnail(_flow(), str(out_dir)) == str(out_dir / "a.png")
        # 16:9 at draft quality — a YouTube thumbnail, not a square hd render.
        assert render.call_args[1] == {"ratio": "16:9", "quality": "low"}
        assert (out_dir / "a.png").read_bytes() == b"png"


class TestYouTubePublish:
    def test_skipped_without_credentials(self, tmp_path):
        assert vt.youtube_configured() is False
        with patch("requests.post") as post:
            assert vt.publish_to_youtube(str(tmp_path / "a.mp4"), "t", "d") is None
        post.assert_not_called()

    def _configure(self, monkeypatch):
        _configure_youtube(monkeypatch)

    def test_resumable_upload_returns_the_watch_url(self, tmp_path, monkeypatch):
        self._configure(monkeypatch)
        mp4 = str(tmp_path / "a.mp4")
        Path(mp4).write_bytes(b"\x00")
        token = MagicMock(headers={}, **{"json.return_value": {"access_token": "at"}})
        session = MagicMock(headers={"Location": "https://upload.test/x"})
        session.json.return_value = {}
        done = MagicMock()
        done.json.return_value = {"id": "vid123"}
        with patch("requests.post", side_effect=[token, session]) as post, \
             patch("requests.put", return_value=done):
            url = vt.publish_to_youtube(mp4, "Title", "Body", ["lem"])
        assert url == "https://www.youtube.com/watch?v=vid123"
        assert post.call_args.kwargs["json"]["status"]["privacyStatus"] == "unlisted"

    def test_upload_failure_returns_none_rather_than_losing_the_render(self, tmp_path, monkeypatch):
        self._configure(monkeypatch)
        with patch("requests.post", side_effect=RuntimeError("network down")):
            assert vt.publish_to_youtube(str(tmp_path / "a.mp4"), "t", "d") is None

    def test_missing_upload_location_is_reported_as_a_failure(self, tmp_path, monkeypatch):
        self._configure(monkeypatch)
        token = MagicMock(headers={}, **{"json.return_value": {"access_token": "at"}})
        session = MagicMock(headers={})
        with patch("requests.post", side_effect=[token, session]):
            assert vt.publish_to_youtube(str(tmp_path / "a.mp4"), "t", "d") is None


class TestProduceTutorial:
    def _patched(self, monkeypatch, duration="4.0"):
        monkeypatch.setattr(vt.shutil, "which", lambda name: f"/usr/bin/{name}")
        element = MagicMock()
        element.text = "Powerful Features"
        return element

    def test_disabled_pipeline_produces_nothing(self, monkeypatch):
        monkeypatch.setenv("TUTORIAL_VIDEOS_ENABLED", "false")
        assert vt.produce_tutorial() is None

    def test_unknown_flow_key_raises(self):
        with pytest.raises(ValueError, match="Unknown tutorial flow"):
            vt.produce_tutorial(flow_key="nope")

    def test_auth_flow_requested_without_a_session_is_skipped(self):
        auth_key = next(k for k, f in vt.TUTORIAL_FLOWS.items() if f.requires_auth)
        assert vt.produce_tutorial(flow_key=auth_key) is None

    def test_nothing_to_do_when_every_flow_is_current(self, monkeypatch):
        vt.save_manifest({"videos": {k: {"produced_at": "2999-01-01T00:00:00+00:00"}
                                     for k, f in vt.TUTORIAL_FLOWS.items()
                                     if not f.requires_auth}})
        assert vt.produce_tutorial() is None

    def test_end_to_end_produces_publishes_and_prices_one_tutorial(self, monkeypatch):
        element = self._patched(monkeypatch)
        _configure_youtube(monkeypatch)
        flow = vt.next_flow({"videos": {}})
        segments = len(flow.steps)
        # Three token-endpoint POSTs now: the #742 publish preflight, then the upload's own token
        # exchange, then the resumable-session start.
        preflight = _token_response(payload={"access_token": "at", "scope": ya.UPLOAD_SCOPE})
        token = MagicMock(headers={}, **{"json.return_value": {"access_token": "at"}})
        session = MagicMock(headers={"Location": "https://upload.test/x"})
        done = MagicMock()
        done.json.return_value = {"id": "vid999"}

        with patch("cqc_lem.utilities.selenium_util.get_docker_driver",
                   return_value=_driver()) as get_driver, \
             patch("cqc_lem.utilities.selenium_util.find_first", return_value=element), \
             patch("cqc_lem.utilities.ai.ai_helper._call_llm",
                   return_value=_llm_response(_script_json(segments))), \
             patch("cqc_lem.utilities.ai.client.client.audio.speech.create",
                   return_value=SimpleNamespace(content=b"mp3")), \
             patch("subprocess.run", side_effect=_fake_ffmpeg("4.0")), \
             patch("requests.post", side_effect=[preflight, token, session]), \
             patch("requests.put", return_value=done), \
             patch(f"{_MOD}.track_media_cost") as track:
            record = vt.produce_tutorial()

        get_driver.assert_called_once()
        assert record["flow"] == flow.key
        assert record["youtube_url"] == "https://www.youtube.com/watch?v=vid999"
        # intro card + one frame per step + outro card, each held for max(declared, narration+pad)
        expected = sum(max(s, 4.0 + vt.SEGMENT_PAD_SECONDS)
                       for s in ([vt.CARD_SECONDS] + [st.seconds for st in flow.steps]
                                 + [vt.CARD_SECONDS]))
        assert record["duration_seconds"] == pytest.approx(expected)
        assert record["narration_chars"] > 0
        assert record["tts_usd"] > 0 and record["render_usd"] > 0
        assert record["total_usd"] == pytest.approx(record["tts_usd"] + record["render_usd"])
        assert record["video_url"].endswith(f"videos/tutorials/{flow.key}/{flow.key}.mp4")
        assert record["vertical_url"].endswith(f"{flow.key}-vertical.mp4")
        assert record["captions_url"].endswith(f"{flow.key}.srt")
        assert record["thumbnail_url"] is None
        # Cost is attributed per part: one audio row per segment + one render row.
        kinds = [c[0][0] for c in track.call_args_list]
        assert kinds.count("audio") == segments + 2 and kinds.count("video") == 1
        # Persisted for the SPA embed, keyed by flow.
        assert vt.load_manifest()["videos"][flow.key]["youtube_url"] == record["youtube_url"]

    def test_second_run_skips_a_flow_whose_ui_has_not_changed(self, monkeypatch):
        element = self._patched(monkeypatch)
        flow = vt.next_flow({"videos": {}})
        vt.save_manifest({"videos": {flow.key: {
            "produced_at": "2999-01-01T00:00:00+00:00",
            "fingerprint": vt.ui_fingerprint(["Powerful Features"] * len(flow.steps)),
        }}})
        with patch("cqc_lem.utilities.selenium_util.get_docker_driver", return_value=_driver()), \
             patch("cqc_lem.utilities.selenium_util.find_first", return_value=element), \
             patch("cqc_lem.utilities.ai.ai_helper._call_llm") as call:
            assert vt.produce_tutorial(flow_key=None) is None
        call.assert_not_called()

    def test_capture_failure_aborts_before_any_llm_or_tts_call(self, monkeypatch):
        self._patched(monkeypatch)
        with patch("cqc_lem.utilities.selenium_util.get_docker_driver", return_value=_driver()), \
             patch("cqc_lem.utilities.selenium_util.find_first", return_value=None), \
             patch("cqc_lem.utilities.ai.ai_helper._call_llm") as call, \
             patch("cqc_lem.utilities.ai.client.client.audio.speech.create") as tts:
            with pytest.raises(vt.TutorialCaptureError):
                vt.produce_tutorial()
        call.assert_not_called()
        tts.assert_not_called()

    def test_guardrail_failure_aborts_before_tts_spend(self, monkeypatch):
        element = self._patched(monkeypatch)
        raw = _script_json(3, outro="We save teams 97 hours a month.")
        with patch("cqc_lem.utilities.selenium_util.get_docker_driver", return_value=_driver()), \
             patch("cqc_lem.utilities.selenium_util.find_first", return_value=element), \
             patch("cqc_lem.utilities.ai.ai_helper._call_llm", return_value=_llm_response(raw)), \
             patch("cqc_lem.utilities.ai.client.client.audio.speech.create") as tts:
            with pytest.raises(vt.TutorialGuardrailError):
                vt.produce_tutorial()
        tts.assert_not_called()

    def test_a_dead_publishing_token_aborts_before_any_capture_or_tts_spend(self, monkeypatch):
        """Issue #742: a render that can't be published is wasted money, so the token is verified
        before the browser opens — not discovered at the upload step after the spend.
        """
        self._patched(monkeypatch)
        _configure_youtube(monkeypatch)
        with patch("requests.post",
                   return_value=_token_response(400, {"error": "invalid_grant"})), \
             patch("cqc_lem.utilities.selenium_util.get_docker_driver") as get_driver, \
             patch("cqc_lem.utilities.ai.ai_helper._call_llm") as call, \
             patch("cqc_lem.utilities.ai.client.client.audio.speech.create") as tts:
            assert vt.produce_tutorial() is None
        get_driver.assert_not_called()
        call.assert_not_called()
        tts.assert_not_called()

    def test_an_unconfigured_install_still_renders_an_unpublished_tutorial(self, monkeypatch):
        """No OAuth credentials is the expected pre-1.0 state — the MP4 is still a usable asset, so
        the preflight must not turn "publishing is off" into "produce nothing".
        """
        element = self._patched(monkeypatch)
        with patch("cqc_lem.utilities.selenium_util.get_docker_driver", return_value=_driver()), \
             patch("cqc_lem.utilities.selenium_util.find_first", return_value=element), \
             patch("cqc_lem.utilities.ai.ai_helper._call_llm",
                   return_value=_llm_response(_script_json(3))), \
             patch("cqc_lem.utilities.ai.client.client.audio.speech.create",
                   return_value=SimpleNamespace(content=b"mp3")), \
             patch("subprocess.run", side_effect=_fake_ffmpeg("4.0")), \
             patch("requests.post") as post:
            record = vt.produce_tutorial()
        assert record is not None and record["youtube_url"] is None
        post.assert_not_called()

    def test_an_undecidable_probe_never_blocks_a_run(self, monkeypatch):
        """Google being unreachable is not evidence the grant is dead — the run proceeds and the
        upload takes its own chances (an upload failure never loses the render).
        """
        element = self._patched(monkeypatch)
        _configure_youtube(monkeypatch)
        with patch("cqc_lem.utilities.selenium_util.get_docker_driver", return_value=_driver()), \
             patch("cqc_lem.utilities.selenium_util.find_first", return_value=element), \
             patch("cqc_lem.utilities.ai.ai_helper._call_llm",
                   return_value=_llm_response(_script_json(3))), \
             patch("cqc_lem.utilities.ai.client.client.audio.speech.create",
                   return_value=SimpleNamespace(content=b"mp3")), \
             patch("subprocess.run", side_effect=_fake_ffmpeg("4.0")), \
             patch("requests.post", side_effect=OSError("network down")):
            record = vt.produce_tutorial()
        assert record is not None and record["youtube_url"] is None
