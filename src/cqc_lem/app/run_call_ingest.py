"""Call / voice-note ingest tasks (issue #2629, Phase 1b).

``ingest_call_transcript`` reads the stored REDACTED own-turn transcript, extracts candidate facts
with ``lem-simple`` (JSON), drops any whose ``source_quote`` does not verify against the
transcript, and writes the rest as PENDING. Nothing pending is readable by the story-bank
selectors. ``purge_expired_call_transcripts`` (beat) enforces the 7-day hard cap and never
consults the ingest kill switch. Never add this feature to ``LLM_PROMPT_LOGGING_FEATURES``.
"""

import os
import tempfile
from typing import Optional

from cqc_lem.app.my_celery import app as shared_task
from cqc_lem.utilities.ai import call_audio
from cqc_lem.utilities.ai.call_ingest import (
    EXTRACT_SYSTEM_PROMPT,
    OWN_KNOWN,
    call_ingest_enabled,
    filter_verified,
    parse_extraction,
    redact,
    select_own_turns,
    transcript_text,
)
from cqc_lem.utilities.ai.client import client
from cqc_lem.utilities.db import (
    clear_call_audio,
    get_call_audio_path,
    get_call_transcript,
    get_voice_enrollment,
    insert_extract_candidates,
    list_expired_call_audio,
    purge_call_transcripts,
    set_call_ingest_status,
    store_call_transcript,
)
from cqc_lem.utilities.logger import log_debug, log_error, log_info
from cqc_lem.utilities.observability import track_llm_call

_MAX_CHARS = 24000
WHISPER_USD_PER_MIN = 0.006


def _extract(transcript: str) -> list:
    response = client.chat.completions.create(
        model="lem-simple",
        messages=[{"role": "system", "content": EXTRACT_SYSTEM_PROMPT},
                  {"role": "user", "content": transcript[:_MAX_CHARS]}],
        temperature=0.2,
        response_format={"type": "json_object"},
    )
    return parse_extraction(response.choices[0].message.content or "")


@shared_task.task(name="cqc_lem.app.run_call_ingest.ingest_call_transcript")
def ingest_call_transcript(user_id: int, ingest_id: int) -> str:
    """Extract, verify and store PENDING candidates for one uploaded call."""
    if not call_ingest_enabled():
        log_debug("Call ingest off — nothing extracted", user_id=user_id,
                  task_name="ingest_call_transcript")
        return "disabled"
    transcript = get_call_transcript(user_id, ingest_id)
    if not transcript:
        return "no transcript"
    try:
        extracted = _extract(transcript)
    except Exception as e:  # noqa: BLE001 - any provider fault fails this one ingest, loudly
        log_error("Call extraction failed", exc=e, user_id=user_id,
                  task_name="ingest_call_transcript")
        set_call_ingest_status(user_id, ingest_id, "failed")
        return "failed"
    kept, dropped = filter_verified(extracted, transcript)
    stored = insert_extract_candidates(user_id, ingest_id, kept)
    set_call_ingest_status(user_id, ingest_id, "extracted")
    log_info("Call candidates stored", user_id=user_id, task_name="ingest_call_transcript",
             stored=stored, dropped=len(dropped))
    return f"{stored} candidates, {len(dropped)} dropped"


def delete_audio_file(path: Optional[str]) -> None:
    """Best-effort removal of a spooled recording. A leftover is caught by the 24 h purge."""
    if not path:
        return
    try:
        os.remove(path)
    except FileNotFoundError:
        log_debug("Call audio already gone", task_name="transcribe_call_audio")
    except OSError as e:
        log_debug("Could not delete call audio", exc=e, task_name="transcribe_call_audio")


def _fail_audio(user_id: int, ingest_id: int, path: Optional[str]) -> str:
    """Drop the recording and mark the call failed — audio never outlives a failed attempt."""
    delete_audio_file(path)
    clear_call_audio(user_id, ingest_id)
    set_call_ingest_status(user_id, ingest_id, "failed")
    return "failed"


def _book_transcription_spend(user_id: int, seconds: float, success: bool) -> None:
    """Accrue the Whisper bill (a per-minute price, no tokens) to the daily cost rollup."""
    try:
        track_llm_call("lem-transcribe", 0, 0, 0, success=success, user_id=user_id,
                       feature="call_ingest", response_cost=(seconds / 60.0) * WHISPER_USD_PER_MIN)
    except Exception as e:  # noqa: BLE001 - bookkeeping must never cost the transcript
        log_debug("Could not book transcription spend", exc=e, user_id=user_id)


@shared_task.task(name="cqc_lem.app.run_call_ingest.transcribe_call_audio")
def transcribe_call_audio(user_id: int, ingest_id: int) -> str:
    """Transcribe an uploaded recording, keep only the user's own turns, then hand off to extraction.

    Order is the privacy contract: decode and duration-check BEFORE any spend, label every segment
    by voice, drop everything that is not the user's (an UNKNOWN segment included), redact, store
    the transcript, and only then delete the audio. Any failure deletes the audio too.
    """
    path = get_call_audio_path(user_id, ingest_id)
    if not call_ingest_enabled():
        delete_audio_file(path)
        clear_call_audio(user_id, ingest_id)
        return "disabled"
    if not path:
        return "no audio"
    enrolled = get_voice_enrollment(user_id)
    if not enrolled:
        log_info("Call audio has no voice enrollment — not transcribed", user_id=user_id,
                 task_name="transcribe_call_audio")
        return _fail_audio(user_id, ingest_id, path)
    try:
        engine = call_audio.speaker_engine()
        samples = call_audio.decode_audio(path)
        segments = call_audio.transcribe_segments(
            samples, lambda chunk: call_audio.write_wav(chunk, _temp_wav()))
        _book_transcription_spend(user_id, call_audio.duration_seconds(samples), True)
        turns = call_audio.label_turns(samples, segments, enrolled, engine)
    except call_audio.SpeakerSetupError as e:
        log_error("Call speaker ID is not set up", exc=e, user_id=user_id,
                  task_name="transcribe_call_audio")
        return _fail_audio(user_id, ingest_id, path)
    except call_audio.AudioError as e:
        log_debug("Call audio rejected", exc=e, user_id=user_id, task_name="transcribe_call_audio")
        return _fail_audio(user_id, ingest_id, path)
    except Exception as e:  # noqa: BLE001 - a provider or decode fault fails this one ingest, loudly
        log_error("Call transcription failed", exc=e, user_id=user_id,
                  task_name="transcribe_call_audio")
        return _fail_audio(user_id, ingest_id, path)
    own, state = select_own_turns(turns, call_audio.OWN_SPEAKER)
    own = [{**t, "text": redact(t.get("text") or "")[0]} for t in own]
    text = transcript_text(own)
    if state != OWN_KNOWN or not text.strip():
        log_info("No turn matched the user's voice — nothing stored", user_id=user_id,
                 task_name="transcribe_call_audio")
        return _fail_audio(user_id, ingest_id, path)
    if not store_call_transcript(user_id, ingest_id, text, call_audio.OWN_SPEAKER, len(own)):
        return _fail_audio(user_id, ingest_id, path)
    delete_audio_file(path)
    ingest_call_transcript.apply_async(kwargs={"user_id": user_id, "ingest_id": ingest_id})
    return f"{len(own)} own turns"


def _temp_wav() -> str:
    fd, name = tempfile.mkstemp(prefix="call_chunk_", suffix=".wav")
    os.close(fd)
    return name


@shared_task.task(name="cqc_lem.app.run_call_ingest.purge_expired_call_transcripts")
def purge_expired_call_transcripts() -> str:
    """Daily: null every transcript past its hard cap, delete stranded audio past 24 h.

    Runs whatever the flag says.
    """
    purged = purge_call_transcripts()
    stale = list_expired_call_audio()
    for owner, ingest_id, audio_path in stale:
        delete_audio_file(audio_path)
        clear_call_audio(owner, ingest_id)
    if purged or stale:
        log_info("Expired call data purged", count=purged, audio=len(stale),
                 task_name="purge_expired_call_transcripts")
    return f"purged {purged}, audio {len(stale)}"
