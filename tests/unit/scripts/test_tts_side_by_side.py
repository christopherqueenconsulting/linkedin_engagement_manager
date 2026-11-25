"""Pins `scripts/tts_side_by_side.py` (#2258): both TTS calls mocked, no network."""

import importlib.util
import pathlib

import pytest

pytestmark = pytest.mark.unit

_SCRIPT = pathlib.Path("scripts/tts_side_by_side.py")


@pytest.fixture
def script(monkeypatch):
    spec = importlib.util.spec_from_file_location("tts_side_by_side", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod.vt, "audio_duration", lambda p: 12.5)
    return mod


def _fake_openai(text, path):
    pathlib.Path(path).write_bytes(b"a")


def _fake_realtime(text, path):
    pathlib.Path(path).write_bytes(b"b")
    return 0.0123


def _boom(message):
    def raiser(*_args):
        raise RuntimeError(message)
    return raiser


def test_sample_fits_narration_cap(script):
    assert len(script.SAMPLE_NARRATION) <= script.TUTORIAL_MAX_NARRATION_CHARS


def test_renders_both_files_and_reports(script, monkeypatch, tmp_path, capsys):
    calls = []

    def openai(t, p):
        calls.append(t)
        _fake_openai(t, p)

    def realtime(t, p):
        calls.append(t)
        return _fake_realtime(t, p)

    monkeypatch.setattr(script.vt, "_tts_openai", openai)
    monkeypatch.setattr(script.vt, "_tts_realtime", realtime)
    assert script.main([str(tmp_path / "out")]) == 0
    assert calls == [script.SAMPLE_NARRATION] * 2
    assert (tmp_path / "out" / "tts-1.mp3").exists()
    assert (tmp_path / "out" / "realtime.mp3").exists()
    out = capsys.readouterr().out
    assert "tts-1:" in out and "realtime:" in out
    assert "duration=12.5s" in out and f"chars={len(script.SAMPLE_NARRATION)}" in out
    assert "usd=0.012300" in out


def test_realtime_failure_keeps_tts1_and_exits_nonzero(script, monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(script.vt, "_tts_openai", _fake_openai)
    monkeypatch.setattr(script.vt, "_tts_realtime", _boom("session refused"))
    assert script.main([str(tmp_path)]) == 1
    assert (tmp_path / "tts-1.mp3").exists()
    assert "session refused" in capsys.readouterr().err


def test_tts1_failure_exits_two(script, monkeypatch, tmp_path):
    monkeypatch.setattr(script.vt, "_tts_openai", _boom("no key"))
    assert script.main([str(tmp_path)]) == 2


def test_bad_usage(script):
    assert script.main([]) == 64


def test_oversize_sample_refused(script, monkeypatch, tmp_path):
    monkeypatch.setattr(script, "TUTORIAL_MAX_NARRATION_CHARS", 5)
    assert script.main([str(tmp_path)]) == 64


def test_duration_failure_does_not_hide_render(script, monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(script.vt, "_tts_openai", _fake_openai)
    monkeypatch.setattr(script.vt, "_tts_realtime", _fake_realtime)
    monkeypatch.setattr(script.vt, "audio_duration", _boom("no ffprobe"))
    assert script.main([str(tmp_path)]) == 0
    assert "unreadable" in capsys.readouterr().out
