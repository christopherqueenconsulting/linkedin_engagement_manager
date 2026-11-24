"""Every SQL statement behind `voice_exemplars` (issue #2627, Phase 1a).

Re-exported by `cqc_lem.utilities.db`; a test patches the name HERE, where it lives.

Writes fail soft and report a count, reads return None on a fault: "no exemplars" and "could not read
them" are different answers, and only the first may fall back to the adjective brief silently.
"""

import hashlib
import json
from typing import Optional

import mysql.connector

from cqc_lem.platform.db.connection import db_cursor
from cqc_lem.utilities.logger import log_error

VOICE_EXEMPLAR_SOURCES = ("comments", "shares")
# Hard cap on stored rows per user: the table is read whole to rank by cosine, so it stays small.
MAX_EXEMPLARS_PER_USER = 500


def exemplar_hash(text: str) -> str:
    """Dedupe key for an exemplar: sha256 of its whitespace- and case-normalised text."""
    return hashlib.sha256(" ".join((text or "").lower().split()).encode("utf-8")).hexdigest()


def count_voice_exemplars(user_id: int) -> Optional[int]:
    """How many exemplars the user has stored. None on a read fault."""
    try:
        with db_cursor() as cursor:
            cursor.execute("SELECT COUNT(*) FROM voice_exemplars WHERE user_id = %s", (user_id,))
            row = cursor.fetchone()
            return int(row[0]) if row else 0
    except mysql.connector.Error as err:
        log_error("Could not count voice exemplars", exc=err, user_id=user_id)
        return None


def insert_voice_exemplar(user_id: int, source: str, text: str,
                          embedding: Optional[list]) -> bool:
    """Store one exemplar. True only when a NEW row was written (a duplicate is a no-op)."""
    if source not in VOICE_EXEMPLAR_SOURCES or not (text or "").strip():
        return False
    try:
        with db_cursor(commit=True) as cursor:
            cursor.execute(
                "INSERT IGNORE INTO voice_exemplars (user_id, source, text_hash, body, embedding) "
                "VALUES (%s,%s,%s,%s,%s)",
                (user_id, source, exemplar_hash(text), text,
                 json.dumps(embedding) if embedding else None))
            return cursor.rowcount >= 1
    except mysql.connector.Error as err:
        log_error("Could not store a voice exemplar", exc=err, user_id=user_id)
        return False


def get_existing_exemplar_hashes(user_id: int) -> Optional[set]:
    """The text hashes already stored for the user. None on a read fault."""
    try:
        with db_cursor() as cursor:
            cursor.execute("SELECT text_hash FROM voice_exemplars WHERE user_id = %s", (user_id,))
            return {r[0] for r in cursor.fetchall()}
    except mysql.connector.Error as err:
        log_error("Could not read voice exemplar hashes", exc=err, user_id=user_id)
        return None


def get_embedded_voice_exemplars(user_id: int) -> Optional[list]:
    """Every exemplar that has a vector, as `{"body", "embedding"}`. None on a read fault."""
    try:
        with db_cursor(dictionary=True) as cursor:
            cursor.execute(
                "SELECT body, embedding FROM voice_exemplars WHERE user_id = %s "
                "AND embedding IS NOT NULL LIMIT %s", (user_id, MAX_EXEMPLARS_PER_USER))
            rows = []
            for r in cursor.fetchall():
                raw = r["embedding"]
                vec = json.loads(raw) if isinstance(raw, (str, bytes)) else raw
                if vec:
                    rows.append({"body": r["body"], "embedding": vec})
            return rows
    except (mysql.connector.Error, ValueError) as err:
        log_error("Could not read voice exemplars", exc=err, user_id=user_id)
        return None
