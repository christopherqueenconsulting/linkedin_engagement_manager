"""Call / voice-note ingest tasks (issue #2629, Phase 1b).

``ingest_call_transcript`` reads the stored REDACTED own-turn transcript, extracts candidate facts
with ``lem-simple`` (JSON), drops any whose ``source_quote`` does not verify against the
transcript, and writes the rest as PENDING. Nothing pending is readable by the story-bank
selectors. ``purge_expired_call_transcripts`` (beat) enforces the 7-day hard cap and never
consults the ingest kill switch. Never add this feature to ``LLM_PROMPT_LOGGING_FEATURES``.
"""

from cqc_lem.app.my_celery import app as shared_task
from cqc_lem.utilities.ai.call_ingest import (
    EXTRACT_SYSTEM_PROMPT,
    call_ingest_enabled,
    filter_verified,
    parse_extraction,
)
from cqc_lem.utilities.ai.client import client
from cqc_lem.utilities.db import (
    get_call_transcript,
    insert_extract_candidates,
    purge_call_transcripts,
    set_call_ingest_status,
)
from cqc_lem.utilities.logger import log_debug, log_error, log_info

_MAX_CHARS = 24000


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


@shared_task.task(name="cqc_lem.app.run_call_ingest.purge_expired_call_transcripts")
def purge_expired_call_transcripts() -> str:
    """Daily: null every transcript past its hard cap. Runs whatever the flag says."""
    purged = purge_call_transcripts()
    if purged:
        log_info("Expired call transcripts purged", count=purged,
                 task_name="purge_expired_call_transcripts")
    return f"purged {purged}"
