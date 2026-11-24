"""Every SQL statement behind call / voice-note ingest (issue #2629, Phase 1a).

Re-exported by `cqc_lem.utilities.db`. The transcript column is sealed with the envelope scheme
under the named AAD constant below — renaming it orphans every stored transcript.

Fail-soft like the rest of the facade: a DB fault logs and returns the empty value, with ONE
deliberate exception — `insert_call_ingest` returns None on a fault so the caller never believes a
transcript was stored when it was not.
"""

import hashlib
import json
from datetime import datetime, timedelta
from typing import Optional

import mysql.connector

from cqc_lem.platform.db.connection import db_cursor
from cqc_lem.platform.db.repositories.feedback import _LEN_STORY_TITLE, STORY_BANK_KINDS
from cqc_lem.utilities.crypto import decrypt_secret, encrypt_secret
from cqc_lem.utilities.logger import log_error

CALL_TRANSCRIPT_FIELD = "call_ingest.transcript"
CALL_VOICE_FIELD = "call_ingest.voice_embedding"
CALL_SOURCE_TYPES = ("otter", "wispr", "audio")
AUDIO_FAILURE_CAP_HOURS = 24
CANDIDATE_REVIEW_STATUSES = ("accepted", "edited", "rejected")
STORY_BANK_ORIGIN_CALL = "call"
RETENTION_HARD_CAP_DAYS = 7


def transcript_hash(transcript: str) -> str:
    """Stable dedupe key for a transcript (one row per user + content)."""
    return hashlib.sha256((transcript or "").encode("utf-8")).hexdigest()


def create_call_source(user_id: int, source_type: str, label: Optional[str] = None,
                       consent_attested: bool = False) -> Optional[int]:
    """Register a source. It is enabled only when the consent attestation is given."""
    if source_type not in CALL_SOURCE_TYPES:
        return None
    try:
        with db_cursor(commit=True) as cursor:
            cursor.execute(
                "INSERT INTO call_source (user_id, source_type, label, consent_attested, "
                "consent_attested_at, enabled) VALUES (%s,%s,%s,%s,%s,%s)",
                (user_id, source_type, (label or None), 1 if consent_attested else 0,
                 datetime.utcnow() if consent_attested else None, 1 if consent_attested else 0))
            return int(cursor.lastrowid)
    except mysql.connector.Error as err:
        log_error("Could not create call source", exc=err, user_id=user_id)
        return None


def is_call_source_enabled(user_id: int, source_id: int) -> bool:
    """True only for the user's own source that is attested AND enabled. A fault reads as False."""
    try:
        with db_cursor() as cursor:
            cursor.execute(
                "SELECT enabled, consent_attested FROM call_source WHERE id=%s AND user_id=%s",
                (int(source_id), user_id))
            row = cursor.fetchone()
            return bool(row and row[0] and row[1])
    except mysql.connector.Error as err:
        log_error("Could not read call source", exc=err, user_id=user_id)
        return False


def insert_call_ingest(user_id: int, redacted_transcript: str, source_id: Optional[int] = None,
                       title: Optional[str] = None, own_speaker: Optional[str] = None,
                       turn_count: int = 0, now: Optional[datetime] = None) -> Optional[int]:
    """Store one REDACTED transcript, encrypted, with a hard `purge_after` cap.

    Returns the new id, the existing id for a duplicate (same user + content), or None on a fault.
    """
    stamp = now or datetime.utcnow()
    digest = transcript_hash(redacted_transcript)
    sealed = encrypt_secret(redacted_transcript, user_id, CALL_TRANSCRIPT_FIELD)
    try:
        with db_cursor(commit=True) as cursor:
            cursor.execute(
                "SELECT id FROM call_ingest WHERE user_id=%s AND content_hash=%s",
                (user_id, digest))
            existing = cursor.fetchone()
            if existing:
                return int(existing[0])
            cursor.execute(
                "INSERT INTO call_ingest (user_id, source_id, title, content_hash, transcript_enc, "
                "own_speaker, turn_count, purge_after) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                (user_id, source_id, (title or None), digest, sealed, own_speaker, int(turn_count),
                 stamp + timedelta(days=RETENTION_HARD_CAP_DAYS)))
            return int(cursor.lastrowid)
    except mysql.connector.Error as err:
        log_error("Could not store call ingest", exc=err, user_id=user_id)
        return None


def get_call_transcript(user_id: int, ingest_id: int) -> Optional[str]:
    """The decrypted redacted transcript, or None when purged, missing or unreadable."""
    try:
        with db_cursor() as cursor:
            cursor.execute(
                "SELECT transcript_enc FROM call_ingest WHERE id=%s AND user_id=%s",
                (int(ingest_id), user_id))
            row = cursor.fetchone()
    except mysql.connector.Error as err:
        log_error("Could not read call ingest", exc=err, user_id=user_id)
        return None
    if not row or not row[0]:
        return None
    return decrypt_secret(row[0], user_id, CALL_TRANSCRIPT_FIELD)


def insert_extract_candidates(user_id: int, ingest_id: int, candidates: list) -> int:
    """Insert VERIFIED candidates as `pending`. Returns how many rows were written."""
    rows = []
    for c in candidates or []:
        quote = str(c.get("source_quote") or "").strip()
        body = str(c.get("body") or "").strip()
        if not quote or not body:
            continue
        kind = c.get("kind") if c.get("kind") in STORY_BANK_KINDS else "anecdote"
        title = (str(c.get("title") or "").strip() or body[:80])[:_LEN_STORY_TITLE]
        rows.append((user_id, int(ingest_id), kind, title, body, quote, c.get("t0"),
                     float(c.get("confidence") or 0)))
    if not rows:
        return 0
    try:
        with db_cursor(commit=True) as cursor:
            cursor.executemany(
                "INSERT INTO extract_candidate (user_id, ingest_id, kind, title, body, "
                "source_quote, t0, confidence) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)", rows)
            return len(rows)
    except mysql.connector.Error as err:
        log_error("Could not store extract candidates", exc=err, user_id=user_id)
        return 0


def get_pending_candidates(user_id: int, limit: int = 100) -> list:
    """The user's review queue, newest call first."""
    try:
        with db_cursor(dictionary=True) as cursor:
            cursor.execute(
                "SELECT id, ingest_id, kind, title, body, source_quote, t0, confidence, status "
                "FROM extract_candidate WHERE user_id=%s AND status='pending' "
                "ORDER BY ingest_id DESC, id ASC LIMIT %s", (user_id, int(limit)))
            return list(cursor.fetchall() or [])
    except mysql.connector.Error as err:
        log_error("Could not list extract candidates", exc=err, user_id=user_id)
        return []


def review_candidate(user_id: int, candidate_id: int, decision: str,
                     edited_body: Optional[str] = None) -> Optional[int]:
    """Apply a review decision. `accepted`/`edited` promote to `story_bank` and return its id.

    Only a `pending` candidate owned by `user_id` moves, so a replay or a foreign id changes
    nothing. A rejected candidate stays `rejected` and never returns to the queue.
    """
    if decision not in CANDIDATE_REVIEW_STATUSES:
        return None
    try:
        with db_cursor(dictionary=True, commit=True) as cursor:
            cursor.execute(
                "SELECT id, ingest_id, kind, title, body, source_quote, t0 FROM extract_candidate "
                "WHERE id=%s AND user_id=%s AND status='pending' FOR UPDATE",
                (int(candidate_id), user_id))
            cand = cursor.fetchone()
            if not cand:
                return None
            bank_id = None
            if decision == "rejected":
                cursor.execute(
                    "UPDATE extract_candidate SET status='rejected', reviewed_at=NOW() WHERE id=%s",
                    (cand["id"],))
                return None
            body = str(edited_body).strip() if decision == "edited" and edited_body else cand["body"]
            cursor.execute(
                "INSERT INTO story_bank (user_id, kind, title, body, active, origin, ingest_id, "
                "source_quote, candidate_id, review_status) "
                "VALUES (%s,%s,%s,%s,1,%s,%s,%s,%s,'accepted')",
                (user_id, cand["kind"], cand["title"], body, STORY_BANK_ORIGIN_CALL,
                 cand["ingest_id"], cand["source_quote"], cand["id"]))
            bank_id = int(cursor.lastrowid)
            cursor.execute(
                "UPDATE extract_candidate SET status=%s, story_bank_id=%s, reviewed_at=NOW() "
                "WHERE id=%s", (decision, bank_id, cand["id"]))
            return bank_id
    except mysql.connector.Error as err:
        log_error("Could not review extract candidate", exc=err, user_id=user_id)
        return None


def purge_call_transcripts(now: Optional[datetime] = None) -> int:
    """Null every transcript past its `purge_after` cap. Derived rows keep only `source_quote`.

    Runs regardless of the ingest kill switch. Returns the number of transcripts purged.
    """
    try:
        with db_cursor(commit=True) as cursor:
            cursor.execute(
                "UPDATE call_ingest SET transcript_enc=NULL, status='purged', purged_at=NOW() "
                "WHERE transcript_enc IS NOT NULL AND purge_after <= %s",
                (now or datetime.utcnow(),))
            return int(cursor.rowcount or 0)
    except mysql.connector.Error as err:
        log_error("Could not purge call transcripts", exc=err)
        return 0


def purge_reviewed_call_transcripts(user_id: int) -> int:
    """Null the transcript of every EXTRACTED ingest whose candidates have all been reviewed.

    An ingest still `received` has no candidates yet, so "none pending" is vacuous: purging it
    would delete the transcript before extraction runs.
    """
    try:
        with db_cursor(commit=True) as cursor:
            cursor.execute(
                "UPDATE call_ingest ci SET ci.transcript_enc=NULL, ci.status='purged', "
                "ci.purged_at=NOW() WHERE ci.user_id=%s AND ci.transcript_enc IS NOT NULL "
                "AND ci.status IN ('extracted','reviewed') "
                "AND NOT EXISTS (SELECT 1 FROM extract_candidate ec "
                "WHERE ec.ingest_id=ci.id AND ec.status='pending')", (user_id,))
            return int(cursor.rowcount or 0)
    except mysql.connector.Error as err:
        log_error("Could not purge reviewed call transcripts", exc=err, user_id=user_id)
        return 0


def purge_all_call_data(user_id: int) -> bool:
    """Delete every call row for the user, including promoted story-bank rows (origin='call')."""
    try:
        with db_cursor(commit=True) as cursor:
            cursor.execute("DELETE FROM story_bank WHERE user_id=%s AND origin=%s",
                           (user_id, STORY_BANK_ORIGIN_CALL))
            cursor.execute("DELETE FROM extract_candidate WHERE user_id=%s", (user_id,))
            cursor.execute("DELETE FROM call_ingest WHERE user_id=%s", (user_id,))
            cursor.execute("DELETE FROM call_source WHERE user_id=%s", (user_id,))
            cursor.execute("DELETE FROM call_voice_enrollment WHERE user_id=%s", (user_id,))
            return True
    except mysql.connector.Error as err:
        log_error("Could not purge call data", exc=err, user_id=user_id)
        return False


def set_call_ingest_status(user_id: int, ingest_id: int, status: str) -> bool:
    """Move the user's own ingest to `extracted` or `failed`. A purged row never comes back."""
    if status not in ("extracted", "failed"):
        return False
    try:
        with db_cursor(commit=True) as cursor:
            cursor.execute(
                "UPDATE call_ingest SET status=%s WHERE id=%s AND user_id=%s AND status<>'purged'",
                (status, int(ingest_id), user_id))
            return bool(cursor.rowcount)
    except mysql.connector.Error as err:
        log_error("Could not update call ingest status", exc=err, user_id=user_id)
        return False


def list_call_ingests(user_id: int, limit: int = 50) -> list:
    """The user's calls, newest first, each with its count of candidates still awaiting review."""
    try:
        with db_cursor(dictionary=True) as cursor:
            cursor.execute(
                "SELECT ci.id, ci.title, ci.status, ci.turn_count, ci.purge_after, ci.created_at, "
                "(SELECT COUNT(*) FROM extract_candidate ec "
                "WHERE ec.ingest_id=ci.id AND ec.status='pending') AS pending_count "
                "FROM call_ingest ci WHERE ci.user_id=%s ORDER BY ci.id DESC LIMIT %s",
                (user_id, int(limit)))
            return list(cursor.fetchall() or [])
    except mysql.connector.Error as err:
        log_error("Could not list call ingests", exc=err, user_id=user_id)
        return []


def get_candidates_for_call(user_id: int, ingest_id: int) -> list:
    """The pending candidates of ONE call (the user's own only), in transcript order."""
    try:
        with db_cursor(dictionary=True) as cursor:
            cursor.execute(
                "SELECT id, ingest_id, kind, title, body, source_quote, t0, confidence, status "
                "FROM extract_candidate WHERE user_id=%s AND ingest_id=%s AND status='pending' "
                "ORDER BY id ASC", (user_id, int(ingest_id)))
            return list(cursor.fetchall() or [])
    except mysql.connector.Error as err:
        log_error("Could not list call candidates", exc=err, user_id=user_id)
        return []


def accept_candidates_above(user_id: int, min_confidence: float,
                            ingest_id: Optional[int] = None) -> int:
    """Bulk-accept the user's pending candidates at or above `min_confidence`. Returns the count."""
    sql = ("SELECT id FROM extract_candidate WHERE user_id=%s AND status='pending' "
           "AND confidence>=%s")
    params: list = [user_id, float(min_confidence)]
    if ingest_id is not None:
        sql += " AND ingest_id=%s"
        params.append(int(ingest_id))
    try:
        with db_cursor() as cursor:
            cursor.execute(sql + " ORDER BY id ASC", tuple(params))
            ids = [int(r[0]) for r in (cursor.fetchall() or [])]
    except mysql.connector.Error as err:
        log_error("Could not list candidates to accept", exc=err, user_id=user_id)
        return 0
    return sum(1 for cid in ids if review_candidate(user_id, cid, "accepted"))


def save_voice_enrollment(user_id: int, embedding: list) -> bool:
    """Seal and upsert the user's own-voice embedding (one row per user)."""
    sealed = encrypt_secret(json.dumps([float(x) for x in embedding]), user_id, CALL_VOICE_FIELD)
    try:
        with db_cursor(commit=True) as cursor:
            cursor.execute(
                "INSERT INTO call_voice_enrollment (user_id, embedding_enc) VALUES (%s,%s) "
                "ON DUPLICATE KEY UPDATE embedding_enc=VALUES(embedding_enc)", (user_id, sealed))
            return True
    except mysql.connector.Error as err:
        log_error("Could not store voice enrollment", exc=err, user_id=user_id)
        return False


def get_voice_enrollment(user_id: int) -> Optional[list]:
    """The user's enrolled embedding, or None when absent or unreadable (never a guess)."""
    try:
        with db_cursor() as cursor:
            cursor.execute("SELECT embedding_enc FROM call_voice_enrollment WHERE user_id=%s",
                           (user_id,))
            row = cursor.fetchone()
    except mysql.connector.Error as err:
        log_error("Could not read voice enrollment", exc=err, user_id=user_id)
        return None
    if not row or not row[0]:
        return None
    plain = decrypt_secret(row[0], user_id, CALL_VOICE_FIELD)
    try:
        vec = json.loads(plain or "")
    except (TypeError, ValueError):
        return None
    return vec if isinstance(vec, list) and vec else None


def insert_call_audio_ingest(user_id: int, audio_hash: str, audio_path: str,
                             source_id: Optional[int] = None, title: Optional[str] = None,
                             now: Optional[datetime] = None) -> Optional[int]:
    """Register an uploaded recording awaiting transcription. Same bytes twice returns the same id.

    The transcript is NULL until the worker writes it; `audio_purge_after` is the 24 h cap.
    """
    stamp = now or datetime.utcnow()
    try:
        with db_cursor(commit=True) as cursor:
            cursor.execute("SELECT id, status FROM call_ingest WHERE user_id=%s AND content_hash=%s",
                           (user_id, audio_hash))
            existing = cursor.fetchone()
            if existing:
                if existing[1] == "failed":
                    # A failed attempt deleted its audio, so a re-upload is a retry, not a duplicate.
                    cursor.execute(
                        "UPDATE call_ingest SET status='received', audio_path=%s, "
                        "audio_purge_after=%s WHERE id=%s AND user_id=%s AND status='failed' "
                        "AND transcript_enc IS NULL",
                        (audio_path, stamp + timedelta(hours=AUDIO_FAILURE_CAP_HOURS),
                         int(existing[0]), user_id))
                return int(existing[0])
            cursor.execute(
                "INSERT INTO call_ingest (user_id, source_id, title, content_hash, audio_path, "
                "audio_purge_after, purge_after) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                (user_id, source_id, (title or None), audio_hash, audio_path,
                 stamp + timedelta(hours=AUDIO_FAILURE_CAP_HOURS),
                 stamp + timedelta(days=RETENTION_HARD_CAP_DAYS)))
            return int(cursor.lastrowid)
    except mysql.connector.Error as err:
        log_error("Could not store call audio ingest", exc=err, user_id=user_id)
        return None


def get_call_audio_path(user_id: int, ingest_id: int) -> Optional[str]:
    """The spooled recording path for the user's own ingest, or None once deleted."""
    try:
        with db_cursor() as cursor:
            cursor.execute("SELECT audio_path FROM call_ingest WHERE id=%s AND user_id=%s",
                           (int(ingest_id), user_id))
            row = cursor.fetchone()
            return str(row[0]) if row and row[0] else None
    except mysql.connector.Error as err:
        log_error("Could not read call audio path", exc=err, user_id=user_id)
        return None


def store_call_transcript(user_id: int, ingest_id: int, redacted_transcript: str,
                          own_speaker: str, turn_count: int) -> bool:
    """Write the redacted own-turn transcript and clear the audio reference in ONE statement.

    The caller deletes the file after this returns True, so a transcript never exists beside a
    recording the row still points at.
    """
    sealed = encrypt_secret(redacted_transcript, user_id, CALL_TRANSCRIPT_FIELD)
    try:
        with db_cursor(commit=True) as cursor:
            cursor.execute(
                "UPDATE call_ingest SET transcript_enc=%s, own_speaker=%s, turn_count=%s, "
                "audio_path=NULL, audio_purge_after=NULL WHERE id=%s AND user_id=%s "
                "AND status<>'purged'",
                (sealed, own_speaker, int(turn_count), int(ingest_id), user_id))
            return bool(cursor.rowcount)
    except mysql.connector.Error as err:
        log_error("Could not store call transcript", exc=err, user_id=user_id)
        return False


def clear_call_audio(user_id: int, ingest_id: int) -> bool:
    """Drop the audio reference (the caller deletes the file). Used on failure."""
    try:
        with db_cursor(commit=True) as cursor:
            cursor.execute(
                "UPDATE call_ingest SET audio_path=NULL, audio_purge_after=NULL "
                "WHERE id=%s AND user_id=%s", (int(ingest_id), user_id))
            return bool(cursor.rowcount)
    except mysql.connector.Error as err:
        log_error("Could not clear call audio", exc=err, user_id=user_id)
        return False


def list_expired_call_audio(now: Optional[datetime] = None) -> list:
    """`(user_id, ingest_id, audio_path)` for every recording past its 24 h failure cap."""
    try:
        with db_cursor() as cursor:
            cursor.execute(
                "SELECT user_id, id, audio_path FROM call_ingest "
                "WHERE audio_path IS NOT NULL AND audio_purge_after <= %s",
                (now or datetime.utcnow(),))
            return [(int(r[0]), int(r[1]), str(r[2])) for r in (cursor.fetchall() or [])]
    except mysql.connector.Error as err:
        log_error("Could not list expired call audio", exc=err)
        return []


def list_call_audio_paths(user_id: int) -> list:
    """Every recording path the user still has on disk (for the purge-all endpoint)."""
    try:
        with db_cursor() as cursor:
            cursor.execute("SELECT audio_path FROM call_ingest WHERE user_id=%s "
                           "AND audio_path IS NOT NULL", (user_id,))
            return [str(r[0]) for r in (cursor.fetchall() or [])]
    except mysql.connector.Error as err:
        log_error("Could not list call audio paths", exc=err, user_id=user_id)
        return []
