"""Call-ingest audio (issue #2729): transcribe, own-voice labelling, caps, audio deletion."""

import asyncio
import io
import os
from unittest.mock import MagicMock, patch

import numpy as np
import pytest
from fastapi import HTTPException, UploadFile

from cqc_lem.api.routers import user as user_router
from cqc_lem.app import run_call_ingest as rci
from cqc_lem.utilities.ai import call_audio as ca

pytestmark = pytest.mark.unit

_M = "cqc_lem.app.run_call_ingest"
SR = ca.SAMPLE_RATE


def _tone(seconds: float, value: float) -> np.ndarray:
    return np.full(int(seconds * SR), value, dtype=np.float32)


class FakeEngine:
    """Embeds by the sample's mean: 1.0 is the user's voice, -1.0 someone else's."""

    def embed(self, samples, sample_rate=SR):
        return np.array([1.0, 0.0]) if float(np.mean(samples)) > 0 else np.array([-1.0, 0.0])


ENROLLED = [1.0, 0.0]


class TestLabelling:
    def test_cosine_edges(self):
        assert ca.cosine([], []) == 0.0
        assert ca.cosine([1, 0], [1]) == 0.0
        assert ca.cosine([0, 0], [1, 0]) == 0.0
        assert ca.cosine([1, 0], [1, 0]) == pytest.approx(1.0)

    def test_own_voice_matches_other_voice_is_unknown(self):
        assert ca.label_speaker(_tone(3, 0.5), ENROLLED, FakeEngine()) == ca.OWN_SPEAKER
        assert ca.label_speaker(_tone(3, -0.5), ENROLLED, FakeEngine()) == ca.UNKNOWN_SPEAKER

    def test_short_segment_is_unknown_never_guessed(self):
        assert ca.label_speaker(_tone(0.5, 0.5), ENROLLED, FakeEngine()) == ca.UNKNOWN_SPEAKER

    def test_embedding_fault_is_unknown(self):
        engine = MagicMock()
        engine.embed.side_effect = RuntimeError("onnx")
        assert ca.label_speaker(_tone(3, 0.5), ENROLLED, engine) == ca.UNKNOWN_SPEAKER

    def test_accept_threshold_env(self, monkeypatch):
        monkeypatch.setenv("CALL_SPEAKER_ACCEPT", "junk")
        assert ca._accept_threshold() == ca.DEFAULT_ACCEPT
        monkeypatch.setenv("CALL_SPEAKER_ACCEPT", "0.9")
        assert ca._accept_threshold() == 0.9

    def test_diarization_fixture_labels_each_segment(self):
        samples = np.concatenate([_tone(3, 0.5), _tone(3, -0.5), _tone(3, 0.5)])
        segs = [{"start": 0, "end": 3, "text": "mine one"}, {"start": 3, "end": 6, "text": "theirs"},
                {"start": 6, "end": 9, "text": "mine two"}]
        turns = ca.label_turns(samples, segs, ENROLLED, FakeEngine())
        assert [t["speaker"] for t in turns] == ["me", "unknown", "me"]


class TestSetup:
    def test_missing_model_is_one_clear_setup_error(self, monkeypatch):
        monkeypatch.delenv("CALL_SPEAKER_MODEL_PATH", raising=False)
        with pytest.raises(ca.SpeakerSetupError, match="CALL_SPEAKER_MODEL_PATH"):
            ca.speaker_engine()

    def test_missing_package_is_setup_error(self, monkeypatch, tmp_path):
        model = tmp_path / "m.onnx"
        model.write_bytes(b"x")
        monkeypatch.setenv("CALL_SPEAKER_MODEL_PATH", str(model))
        with patch.dict("sys.modules", {"copilot_core": None, "copilot_core.speaker": None,
                                        "copilot_core.speaker.engine": None}):
            with pytest.raises(ca.SpeakerSetupError, match="copilot-core"):
                ca.speaker_engine()

    def test_engine_built_with_model_path(self, monkeypatch, tmp_path):
        model = tmp_path / "m.onnx"
        model.write_bytes(b"x")
        monkeypatch.setenv("CALL_SPEAKER_MODEL_PATH", str(model))
        fake_mod = MagicMock()
        with patch.dict("sys.modules", {"copilot_core": MagicMock(), "copilot_core.speaker": MagicMock(),
                                        "copilot_core.speaker.engine": fake_mod}):
            ca.speaker_engine()
        fake_mod.SpeakerEngine.assert_called_once_with(model_path=str(model))


class TestDecode:
    def _run(self, stdout=b"", rc=0, exc=None):
        proc = MagicMock(returncode=rc, stdout=stdout)
        kw = {"side_effect": exc} if exc else {"return_value": proc}
        return patch("cqc_lem.utilities.ai.call_audio.subprocess.run", **kw)

    def test_decodes_pcm(self):
        pcm = _tone(2, 0.25).tobytes()
        with self._run(pcm):
            out = ca.decode_audio("x.wav")
        assert len(out) == 2 * SR and ca.duration_seconds(out) == 2.0

    def test_duration_cap_refuses_long_audio(self):
        with self._run(_tone(3, 0.1).tobytes()):
            with pytest.raises(ca.AudioError, match="longer"):
                ca.decode_audio("x.wav", max_seconds=2)

    def test_ffmpeg_failure_and_missing_binary(self):
        with self._run(b"", rc=1):
            with pytest.raises(ca.AudioError):
                ca.decode_audio("x.wav")
        with self._run(exc=OSError("no ffmpeg")):
            with pytest.raises(ca.AudioError):
                ca.decode_audio("x.wav")


class TestTranscribe:
    def test_chunks_offset_segments_and_delete_temp_files(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ca, "TRANSCRIBE_CHUNK_SECONDS", 2)
        samples = _tone(5, 0.1)
        made = []

        def encode(chunk):
            p = tmp_path / f"c{len(made)}.wav"
            p.write_bytes(b"RIFF")
            made.append(p)
            return str(p)

        resp = MagicMock()
        resp.segments = [{"start": 0.0, "end": 1.0, "text": " hi "}, {"start": 1, "end": 2, "text": ""}]
        with patch("cqc_lem.utilities.ai.call_audio.client") as cl:
            cl.audio.transcriptions.create.return_value = resp
            out = ca.transcribe_segments(samples, encode)
        assert cl.audio.transcriptions.create.call_args.kwargs["model"] == "lem-transcribe"
        assert [round(s["start"]) for s in out] == [0, 2, 4]
        assert all(not p.exists() for p in made)

    def test_object_segments_and_tiny_tail_skipped(self, tmp_path):
        seg = MagicMock()
        seg.text, seg.start, seg.end = "yo", 0.0, 1.0
        resp = MagicMock(segments=[seg])
        p = tmp_path / "a.wav"
        p.write_bytes(b"x")
        with patch("cqc_lem.utilities.ai.call_audio.client") as cl:
            cl.audio.transcriptions.create.return_value = resp
            assert ca.transcribe_segments(_tone(0.2, 0.1), lambda c: str(p)) == []
            out = ca.transcribe_segments(_tone(1, 0.1), lambda c: str(p))
        assert out[0]["text"] == "yo"

    def test_write_wav_round_trips(self, tmp_path):
        import wave
        path = ca.write_wav(_tone(1, 0.5), str(tmp_path / "o.wav"))
        with wave.open(path) as w:
            assert w.getframerate() == SR and w.getnframes() == SR


@pytest.fixture
def audio(tmp_path, monkeypatch):
    monkeypatch.setenv("CALL_INGEST_ENABLED", "true")
    path = tmp_path / "call.wav"
    path.write_bytes(b"audio")
    return str(path)


def _patch_pipeline(path, segments, samples, enrolled=ENROLLED):
    return [
        patch(f"{_M}.get_call_audio_path", return_value=path),
        patch(f"{_M}.get_voice_enrollment", return_value=enrolled),
        patch(f"{_M}.call_audio.speaker_engine", return_value=FakeEngine()),
        patch(f"{_M}.call_audio.decode_audio", return_value=samples),
        patch(f"{_M}.call_audio.transcribe_segments", return_value=segments),
        patch(f"{_M}.track_llm_call"),
        patch(f"{_M}.clear_call_audio"),
        patch(f"{_M}.set_call_ingest_status"),
        patch(f"{_M}.store_call_transcript", return_value=True),
        patch(f"{_M}.ingest_call_transcript"),
    ]


def _enter(stack, patches):
    return [stack.enter_context(p) for p in patches]


class TestTranscribeTask:
    def test_own_turns_stored_redacted_audio_deleted_then_extraction_queued(self, audio):
        from contextlib import ExitStack
        samples = np.concatenate([_tone(3, 0.5), _tone(3, -0.5)])
        segs = [{"start": 0, "end": 3, "text": "I cut churn. Mail me at a@b.co"},
                {"start": 3, "end": 6, "text": "Their secret"}]
        with ExitStack() as st:
            mocks = _enter(st, _patch_pipeline(audio, segs, samples))
            store, extract = mocks[8], mocks[9]
            out = rci.transcribe_call_audio.run(7, 3)
        assert out == "1 own turns"
        text = store.call_args.args[2]
        assert "churn" in text and "Their secret" not in text and "a@b.co" not in text
        assert not os.path.exists(audio)
        extract.apply_async.assert_called_once_with(kwargs={"user_id": 7, "ingest_id": 3})

    def test_no_turn_matches_stores_nothing_and_deletes_audio(self, audio):
        from contextlib import ExitStack
        segs = [{"start": 0, "end": 3, "text": "someone else"}]
        with ExitStack() as st:
            mocks = _enter(st, _patch_pipeline(audio, segs, _tone(3, -0.5)))
            store, status = mocks[8], mocks[7]
            assert rci.transcribe_call_audio.run(7, 3) == "failed"
        store.assert_not_called()
        status.assert_called_once_with(7, 3, "failed")
        assert not os.path.exists(audio)

    def test_no_enrollment_never_spends_and_deletes_audio(self, audio):
        from contextlib import ExitStack
        with ExitStack() as st:
            mocks = _enter(st, _patch_pipeline(audio, [], _tone(3, 0.5), enrolled=None))
            transcribe = mocks[4]
            assert rci.transcribe_call_audio.run(7, 3) == "failed"
        transcribe.assert_not_called()
        assert not os.path.exists(audio)

    def test_over_cap_audio_fails_before_transcription_spend(self, audio):
        from contextlib import ExitStack
        with ExitStack() as st:
            mocks = _enter(st, _patch_pipeline(audio, [], _tone(3, 0.5)))
            mocks[3].side_effect = ca.AudioError("Recording is longer than 60 minutes")
            transcribe = mocks[4]
            assert rci.transcribe_call_audio.run(7, 3) == "failed"
        transcribe.assert_not_called()
        assert not os.path.exists(audio)

    @pytest.mark.parametrize("exc", [ca.SpeakerSetupError("no model"), RuntimeError("boom")])
    def test_setup_and_provider_faults_fail_and_delete_audio(self, audio, exc):
        from contextlib import ExitStack
        with ExitStack() as st:
            mocks = _enter(st, _patch_pipeline(audio, [], _tone(3, 0.5)))
            mocks[2].side_effect = exc
            assert rci.transcribe_call_audio.run(7, 3) == "failed"
        assert not os.path.exists(audio)

    def test_store_failure_deletes_audio(self, audio):
        from contextlib import ExitStack
        segs = [{"start": 0, "end": 3, "text": "mine"}]
        with ExitStack() as st:
            mocks = _enter(st, _patch_pipeline(audio, segs, _tone(3, 0.5)))
            mocks[8].return_value = False
            assert rci.transcribe_call_audio.run(7, 3) == "failed"
        assert not os.path.exists(audio)

    def test_flag_off_deletes_audio_and_does_nothing(self, audio, monkeypatch):
        monkeypatch.delenv("CALL_INGEST_ENABLED", raising=False)
        with patch(f"{_M}.get_call_audio_path", return_value=audio), \
             patch(f"{_M}.clear_call_audio"), patch(f"{_M}.call_audio.decode_audio") as dec:
            assert rci.transcribe_call_audio.run(7, 3) == "disabled"
        dec.assert_not_called()
        assert not os.path.exists(audio)

    def test_no_audio_row(self, monkeypatch):
        monkeypatch.setenv("CALL_INGEST_ENABLED", "true")
        with patch(f"{_M}.get_call_audio_path", return_value=None):
            assert rci.transcribe_call_audio.run(7, 3) == "no audio"

    def test_spend_is_booked_per_minute(self):
        with patch(f"{_M}.track_llm_call") as track:
            rci._book_transcription_spend(7, 120.0, True)
        assert track.call_args.kwargs["response_cost"] == pytest.approx(0.012)
        with patch(f"{_M}.track_llm_call", side_effect=RuntimeError("x")):
            rci._book_transcription_spend(7, 1.0, True)  # bookkeeping never raises

    def test_purge_beat_deletes_stranded_audio(self, tmp_path):
        stale = tmp_path / "old.wav"
        stale.write_bytes(b"x")
        with patch(f"{_M}.purge_call_transcripts", return_value=0), \
             patch(f"{_M}.list_expired_call_audio", return_value=[(7, 3, str(stale))]), \
             patch(f"{_M}.clear_call_audio") as clear:
            assert rci.purge_expired_call_transcripts.run() == "purged 0, audio 1"
        clear.assert_called_once_with(7, 3)
        assert not stale.exists()

    def test_delete_audio_file_tolerates_missing_and_errors(self, tmp_path):
        rci.delete_audio_file(None)
        rci.delete_audio_file(str(tmp_path / "gone.wav"))
        with patch(f"{_M}.os.remove", side_effect=PermissionError("no")):
            rci.delete_audio_file(str(tmp_path / "x.wav"))


def _upload(name, body):
    return UploadFile(filename=name, file=io.BytesIO(body))


@pytest.fixture
def on(monkeypatch, tmp_path):
    monkeypatch.setenv("CALL_INGEST_ENABLED", "true")
    monkeypatch.setattr(user_router._main, "get_session_user_id", lambda t: 7)
    monkeypatch.setattr(user_router, "_call_audio_dir", lambda: str(tmp_path))


def _post_audio(**kw):
    args = dict(session_token="t", source_id=4, source_type="audio", own_speaker=None, title=None,
                file=_upload("c.m4a", b"audio-bytes"))
    args.update(kw)
    return asyncio.run(user_router.upload_call_transcript_endpoint(**args))


class TestAudioUploadApi:
    def test_queues_transcription_and_keeps_file_for_the_worker(self, on, tmp_path):
        d = user_router._call_db
        with patch.object(d, "is_call_source_enabled", return_value=True), \
             patch.object(d, "get_voice_enrollment", return_value=ENROLLED), \
             patch.object(d, "insert_call_audio_ingest", return_value=9) as ins, \
             patch.object(d, "get_call_audio_path", side_effect=lambda u, i: ins.call_args.args[2]), \
             patch("cqc_lem.app.run_call_ingest.transcribe_call_audio") as task:
            out = _post_audio()
        assert out.detail == {"ingest_id": 9}
        assert os.path.exists(ins.call_args.args[2])
        task.apply_async.assert_called_once_with(kwargs={"user_id": 7, "ingest_id": 9})

    def test_duplicate_recording_keeps_nothing(self, on, tmp_path):
        d = user_router._call_db
        with patch.object(d, "is_call_source_enabled", return_value=True), \
             patch.object(d, "get_voice_enrollment", return_value=ENROLLED), \
             patch.object(d, "insert_call_audio_ingest", return_value=9), \
             patch.object(d, "get_call_audio_path", return_value=None), \
             patch("cqc_lem.app.run_call_ingest.transcribe_call_audio") as task:
            out = _post_audio()
        assert out.detail["duplicate"] is True
        assert list(tmp_path.iterdir()) == []
        task.apply_async.assert_not_called()

    def test_no_enrollment_is_409(self, on):
        d = user_router._call_db
        with patch.object(d, "is_call_source_enabled", return_value=True), \
             patch.object(d, "get_voice_enrollment", return_value=None):
            with pytest.raises(HTTPException) as e:
                _post_audio()
        assert e.value.status_code == 409 and e.value.detail == {"needs_enrollment": True}

    def test_size_cap_empty_and_wrong_source_type(self, on, monkeypatch, tmp_path):
        d = user_router._call_db
        with patch.object(d, "is_call_source_enabled", return_value=True), \
             patch.object(d, "get_voice_enrollment", return_value=ENROLLED):
            monkeypatch.setattr(ca, "MAX_AUDIO_BYTES", 4)
            with pytest.raises(HTTPException) as e:
                _post_audio()
            assert e.value.status_code == 400
            monkeypatch.setattr(ca, "MAX_AUDIO_BYTES", 1000)
            with pytest.raises(HTTPException) as e:
                _post_audio(file=_upload("c.mp3", b""))
            assert e.value.status_code == 400
            with pytest.raises(HTTPException) as e:
                _post_audio(source_type="otter")
            assert e.value.status_code == 400
        assert list(tmp_path.iterdir()) == []

    def test_store_fault_removes_file(self, on, tmp_path):
        d = user_router._call_db
        with patch.object(d, "is_call_source_enabled", return_value=True), \
             patch.object(d, "get_voice_enrollment", return_value=ENROLLED), \
             patch.object(d, "insert_call_audio_ingest", return_value=None):
            with pytest.raises(HTTPException) as e:
                _post_audio()
        assert e.value.status_code == 500 and list(tmp_path.iterdir()) == []

    def test_purge_all_deletes_recordings_on_disk(self, on, tmp_path):
        rec = tmp_path / "r.wav"
        rec.write_bytes(b"x")
        d = user_router._call_db
        with patch.object(d, "list_call_audio_paths", return_value=[str(rec)]), \
             patch.object(d, "purge_all_call_data", return_value=True):
            user_router.purge_all_call_data_endpoint(
                user_router.CallPurgeRequest(session_token="t"))
        assert not rec.exists()


def _enroll(**kw):
    args = dict(session_token="t", file=_upload("me.wav", b"sample"))
    args.update(kw)
    return asyncio.run(user_router.enroll_call_voice_endpoint(**args))


class TestEnrollApi:
    def test_enrolls_and_stores_only_the_embedding(self, on):
        d = user_router._call_db
        with patch.object(ca, "decode_audio", return_value=_tone(8, 0.5)), \
             patch.object(ca, "speaker_engine", return_value=FakeEngine()), \
             patch.object(d, "save_voice_enrollment", return_value=True) as save:
            assert _enroll().detail == {"enrolled": True}
        assert save.call_args.args[0] == 7 and save.call_args.args[1] == [1.0, 0.0]

    def test_flag_off_wrong_type_short_and_oversize(self, on, monkeypatch):
        with patch.object(ca, "decode_audio", return_value=_tone(2, 0.5)), \
             patch.object(ca, "speaker_engine", return_value=FakeEngine()):
            for kw in (dict(file=_upload("me.txt", b"x")), dict()):
                with pytest.raises(HTTPException) as e:
                    _enroll(**kw)
                assert e.value.status_code == 400
        monkeypatch.setattr(ca, "ENROLL_MAX_BYTES", 2)
        with pytest.raises(HTTPException) as e:
            _enroll()
        assert e.value.status_code == 400
        monkeypatch.delenv("CALL_INGEST_ENABLED")
        with pytest.raises(HTTPException) as e:
            _enroll()
        assert e.value.status_code == 404

    def test_undecodable_is_400_missing_engine_is_503_store_fault_is_500(self, on):
        d = user_router._call_db
        with patch.object(ca, "decode_audio", side_effect=ca.AudioError("bad")):
            with pytest.raises(HTTPException) as e:
                _enroll()
            assert e.value.status_code == 400
        with patch.object(ca, "decode_audio", return_value=_tone(8, 0.5)), \
             patch.object(ca, "speaker_engine", side_effect=ca.SpeakerSetupError("x")):
            with pytest.raises(HTTPException) as e:
                _enroll()
            assert e.value.status_code == 503
        with patch.object(ca, "decode_audio", return_value=_tone(8, 0.5)), \
             patch.object(ca, "speaker_engine", return_value=FakeEngine()), \
             patch.object(d, "save_voice_enrollment", return_value=False):
            with pytest.raises(HTTPException) as e:
                _enroll()
            assert e.value.status_code == 500
