"""Voice from the user's own writing (issue #2627, Phase 1a).

Ingest turns a LinkedIn data-export `Comments.csv` / `Shares.csv` into embedded exemplars; retrieval
hands the nearest few to a comment or DM writer as few-shot. Everything is behind `VOICE_EXEMPLARS`
(default OFF) and every failure degrades to "no exemplars", which leaves the prompt byte-identical to
the flag-off one.
"""

import csv
import io
from typing import Optional

from cqc_lem.utilities.ai.content_framework import (
    comment_similarity_max,
    cosine_similarity,
    embed_comments,
)
from cqc_lem.utilities.flags import VOICE_EXEMPLARS, flag_enabled
from cqc_lem.utilities.logger import log_debug, log_info

# Below this many embedded exemplars the adjective brief stays the voice source.
MIN_EXEMPLARS = 4
NEAREST_K = 5
MIN_CHARS = 40
MAX_CHARS = 1500
MAX_PER_UPLOAD = 200
MAX_UPLOAD_BYTES = 5 * 1024 * 1024
# Texts embedded per `lem-embedding` call.
_EMBED_BATCH = 50

# The export's column holding the user's own words, by file kind.
_TEXT_COLUMNS = {"comments": ("Message", "Comment"), "shares": ("ShareCommentary", "Commentary")}


def voice_exemplars_enabled(user_id: Optional[int] = None) -> bool:
    """Read at call time: the flag, PostHog rollout first and the env var as the fallback."""
    return flag_enabled(VOICE_EXEMPLARS, user_id)


def parse_export_csv(kind: str, raw: bytes) -> list:
    """The user's own texts from an export CSV, in file order, or [] for an unreadable file.

    Rows without the expected text column are skipped; nothing here is graded for quality beyond
    length, which `ingest_exemplars` applies.
    """
    columns = _TEXT_COLUMNS.get(kind)
    if not columns:
        return []
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return []
    reader = csv.DictReader(io.StringIO(text))
    column = next((c for c in (reader.fieldnames or []) if c in columns), None)
    if column is None:
        return []
    try:
        return [(row.get(column) or "").strip() for row in reader]
    except csv.Error:
        return []


def ingest_exemplars(user_id: int, kind: str, texts: list) -> dict:
    """Dedupe, length-filter, cap and embed `texts`, then store them. Returns the counts.

    A text whose batch could not be embedded is NOT stored: a row with no vector is never
    retrievable, and storing it would mark it a duplicate of a later, working upload.
    """
    from cqc_lem.utilities import db

    result = {"received": len(texts), "stored": 0, "duplicate": 0, "too_short_or_long": 0,
              "over_cap": 0, "embed_failed": 0}
    existing = db.get_existing_exemplar_hashes(user_id)
    held = db.count_voice_exemplars(user_id)
    if existing is None or held is None:
        result["unavailable"] = True
        return result
    room = max(0, db.MAX_EXEMPLARS_PER_USER - held)
    fresh, seen = [], set(existing)
    for text in texts:
        text = " ".join(str(text or "").split())
        if not MIN_CHARS <= len(text) <= MAX_CHARS:
            result["too_short_or_long"] += 1
            continue
        key = db.exemplar_hash(text)
        if key in seen:
            result["duplicate"] += 1
            continue
        seen.add(key)
        fresh.append(text)
    allowed = min(MAX_PER_UPLOAD, room)
    result["over_cap"] = max(0, len(fresh) - allowed)
    fresh = fresh[:allowed]
    for start in range(0, len(fresh), _EMBED_BATCH):
        batch = fresh[start:start + _EMBED_BATCH]
        vectors = embed_comments(batch)
        if not vectors:
            result["embed_failed"] += len(batch)
            continue
        for text, vector in zip(batch, vectors):
            if db.insert_voice_exemplar(user_id, kind, text, vector):
                result["stored"] += 1
    log_info("Voice exemplars ingested", user_id=user_id, action_type="voice_exemplars",
             **{k: v for k, v in result.items() if isinstance(v, int)})
    return result


def nearest_voice_exemplars(user_id: int, query: str, k: int = NEAREST_K) -> list:
    """The `k` exemplar texts nearest `query` by embedding cosine, best first.

    [] when the flag is off, the user has fewer than `MIN_EXEMPLARS` embedded exemplars, or the
    query cannot be embedded — every one of those means "use the adjective brief".
    """
    if not (query or "").strip() or not voice_exemplars_enabled(user_id):
        return []
    from cqc_lem.utilities import db

    rows = db.get_embedded_voice_exemplars(user_id)
    if not rows or len(rows) < MIN_EXEMPLARS:
        log_debug("Fewer than the minimum voice exemplars — using the voice brief",
                  user_id=user_id, action_type="voice_exemplars")
        return []
    vectors = embed_comments([query])
    if not vectors:
        return []
    ranked = sorted(rows, key=lambda r: cosine_similarity(vectors[0], r["embedding"]), reverse=True)
    return [r["body"] for r in ranked[:k]]


def exemplar_block(exemplars: list) -> str:
    """The few-shot paragraph for a writer prompt; "" for no exemplars (prompt stays unchanged)."""
    if not exemplars:
        return ""
    lines = "\n".join(f"- {e}" for e in exemplars)
    return ("\n\nHow this user actually writes — real comments and posts they wrote themselves. "
            "Match their rhythm, word choice and length. Do NOT copy or closely paraphrase any "
            "of them, and do not reuse their facts as claims about a new topic:\n" + lines + "\n")


def too_close_to_exemplar(draft: str, exemplars: list) -> Optional[dict]:
    """A near-copy of one of the exemplars the writer was shown, or None when the draft is clear.

    Same embedding cosine bar as the comment similarity gate. An unembeddable draft is not
    blocked: the gate fails open here, as it does for recent comments.
    """
    if not exemplars or not (draft or "").strip():
        return None
    vectors = embed_comments([draft] + list(exemplars))
    if not vectors:
        return None
    threshold = comment_similarity_max()
    scores = [cosine_similarity(vectors[0], v) for v in vectors[1:]]
    best = max(range(len(scores)), key=lambda i: scores[i])
    if scores[best] > threshold:
        return {"score": round(scores[best], 4), "threshold": threshold}
    return None
