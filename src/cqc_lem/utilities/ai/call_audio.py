"""Audio half of call ingest (issue #2729): decode, transcribe, tell the user's voice apart.

Pipeline: decode the recording to 16 kHz mono, transcribe it through the LiteLLM proxy
(``lem-transcribe``, Whisper), then label each transcript segment by comparing its voice embedding
with the user's enrolled one. The result is a turn list the Phase 1 ``select_own_turns`` /
``redact_turns`` path consumes, so nothing here skips redaction or review.

* **Never guess whose voice it was.** A segment is the user's only when its cosine similarity to the
  enrollment reaches the accept threshold. A short, unmatched or unembeddable segment is
  ``UNKNOWN_SPEAKER`` and is dropped by the own-turn filter, so a third party's words are never
  stored as the user's.
* Speaker ID is ``copilot_core``'s ``SpeakerEngine`` (CAM++ via sherpa-onnx), imported lazily; the
  ONNX model is baked into the image and located by ``CALL_SPEAKER_MODEL_PATH`` — never fetched at
  runtime. Without it ``speaker_engine`` raises ``SpeakerSetupError`` (one clear message).
"""

import math
import os
import subprocess
from typing import Any, Callable, Optional

from cqc_lem.utilities.ai.client import client
from cqc_lem.utilities.logger import log_debug

SAMPLE_RATE = 16000
OWN_SPEAKER = "me"
UNKNOWN_SPEAKER = "unknown"

AUDIO_SUFFIXES = (".m4a", ".mp3", ".wav", ".webm")
MAX_AUDIO_BYTES = 60 * 1024 * 1024
MAX_AUDIO_SECONDS = 60 * 60
ENROLL_MAX_BYTES = 8 * 1024 * 1024
ENROLL_MIN_SECONDS = 5.0
ENROLL_MAX_SECONDS = 120.0
# Whisper's own upload ceiling is 25 MB, so a long recording goes up as consecutive chunks.
TRANSCRIBE_CHUNK_SECONDS = 10 * 60
MIN_SEGMENT_SECONDS = 1.5
DEFAULT_ACCEPT = 0.65


class SpeakerSetupError(RuntimeError):
    """Speaker ID is not installed or its model is missing — a setup fault, not a bad upload."""


class AudioError(ValueError):
    """The recording cannot be used (unreadable, too long, too short to enrol)."""


def decode_audio(path: str, max_seconds: float = MAX_AUDIO_SECONDS) -> Any:
    """Decode any supported container to a float32 mono 16 kHz numpy array via ffmpeg.

    Raises ``AudioError`` when ffmpeg cannot read it or it runs past ``max_seconds``. The read is
    capped at ``max_seconds`` + 1 s, so an absurd file is rejected without decoding all of it.
    """
    import numpy as np

    cmd = ["ffmpeg", "-v", "error", "-nostdin", "-i", path, "-t", str(int(max_seconds) + 1),
           "-f", "f32le", "-ac", "1", "-ar", str(SAMPLE_RATE), "-"]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=300, check=False)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise AudioError("Could not decode the recording") from e
    if proc.returncode != 0 or not proc.stdout:
        raise AudioError("Could not decode the recording")
    samples = np.frombuffer(proc.stdout, dtype=np.float32)
    if len(samples) / SAMPLE_RATE > max_seconds:
        raise AudioError(f"Recording is longer than {int(max_seconds // 60)} minutes")
    return samples


def duration_seconds(samples: Any) -> float:
    """Length of decoded audio in seconds."""
    return len(samples) / SAMPLE_RATE


def speaker_engine() -> Any:
    """The shared ``SpeakerEngine``, or ``SpeakerSetupError`` when it cannot be built."""
    model = (os.getenv("CALL_SPEAKER_MODEL_PATH") or "").strip()
    if not model or not os.path.isfile(model):
        raise SpeakerSetupError(
            "Speaker ID is not set up: CALL_SPEAKER_MODEL_PATH must name the baked-in CAM++ model")
    try:
        from copilot_core.speaker.engine import SpeakerEngine
    except ImportError as e:
        raise SpeakerSetupError("Speaker ID is not set up: copilot-core[speaker] is missing") from e
    return SpeakerEngine(model_path=model)


def _accept_threshold() -> float:
    try:
        return float(os.getenv("CALL_SPEAKER_ACCEPT") or DEFAULT_ACCEPT)
    except ValueError:
        return DEFAULT_ACCEPT


def embed_voice(samples: Any, engine: Any) -> list:
    """The L2-normalised embedding of ``samples`` as a plain list (JSON-storable).

    The engine standardises length itself, so the input is never padded here.
    """
    return [float(x) for x in engine.embed(samples, SAMPLE_RATE)]


def cosine(a: list, b: list) -> float:
    """Cosine similarity; 0.0 for an empty, mismatched or zero vector."""
    if not a or len(a) != len(b):
        return 0.0
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    if not na or not nb:
        return 0.0
    return sum(x * y for x, y in zip(a, b)) / (na * nb)


def label_speaker(samples: Any, enrolled: list, engine: Any,
                  accept: Optional[float] = None) -> str:
    """``OWN_SPEAKER`` when the segment matches the enrollment, else ``UNKNOWN_SPEAKER``.

    Too short to embed, an embedding fault or any mismatch is UNKNOWN: this never guesses.
    """
    if len(samples) / SAMPLE_RATE < MIN_SEGMENT_SECONDS:
        return UNKNOWN_SPEAKER
    try:
        vec = embed_voice(samples, engine)
    except Exception:  # noqa: BLE001 - an unembeddable segment is unknown, not a failed call
        return UNKNOWN_SPEAKER
    threshold = _accept_threshold() if accept is None else accept
    return OWN_SPEAKER if cosine(vec, enrolled) >= threshold else UNKNOWN_SPEAKER


def transcribe_segments(samples: Any, encode: Callable[[Any], str]) -> list:
    """Whisper segments ``[{start, end, text}]`` for the whole recording, in chunks.

    ``encode(chunk)`` writes a chunk to a temp WAV and returns its path; the file is deleted here
    once the provider has answered. Segment times are offset to the full recording.
    """
    out: list = []
    step = TRANSCRIBE_CHUNK_SECONDS * SAMPLE_RATE
    for offset in range(0, len(samples), step):
        chunk = samples[offset:offset + step]
        if len(chunk) / SAMPLE_RATE < 0.5:
            continue
        path = encode(chunk)
        try:
            with open(path, "rb") as fh:
                resp = client.audio.transcriptions.create(
                    model="lem-transcribe", file=fh, response_format="verbose_json",
                    timestamp_granularities=["segment"])
        finally:
            try:
                os.remove(path)
            except OSError as e:
                log_debug("Could not remove audio chunk", exc=e, task_name="transcribe_call_audio")
        base = offset / SAMPLE_RATE
        for seg in getattr(resp, "segments", None) or []:
            get = (seg.get if isinstance(seg, dict) else lambda k, d=None: getattr(seg, k, d))
            text = str(get("text", "") or "").strip()
            if text:
                out.append({"start": base + float(get("start", 0) or 0),
                            "end": base + float(get("end", 0) or 0), "text": text})
    return out


def write_wav(samples: Any, path: str) -> str:
    """Write float32 mono 16 kHz samples as 16-bit PCM WAV. Returns ``path``."""
    import wave

    import numpy as np

    pcm = (np.clip(samples, -1.0, 1.0) * 32767).astype("<i2")
    with wave.open(path, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(SAMPLE_RATE)
        wav.writeframes(pcm.tobytes())
    return path


def label_turns(samples: Any, segments: list, enrolled: list, engine: Any) -> list:
    """Turn list ``[{speaker, t0, t1, text}]`` with each segment labelled own or unknown."""
    turns = []
    for seg in segments:
        a = max(0, int(seg["start"] * SAMPLE_RATE))
        b = min(len(samples), int(seg["end"] * SAMPLE_RATE))
        who = label_speaker(samples[a:b], enrolled, engine)
        turns.append({"speaker": who, "t0": seg["start"], "t1": seg["end"], "text": seg["text"]})
    return turns
