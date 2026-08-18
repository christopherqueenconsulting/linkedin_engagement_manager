"""Every SQL statement LEM runs against the feedback tables.

Split out of `cqc_lem.utilities.db` (issue #1154). The fail-soft reader contract and the
secret-sealing rules described there apply here unchanged; `cqc_lem.utilities.db`
re-exports every name below, so existing importers and patch targets keep resolving.
"""

import json
from datetime import (
    datetime,
    timezone,
)
from typing import (
    Optional,
    Union,
)

import mysql.connector

from cqc_lem.platform.db import connection as _connection
from cqc_lem.platform.db.connection import db_cursor
from cqc_lem.platform.db.enums import (
    FaqStatus,
    FeedbackSource,
    FeedbackStatus,
)
from cqc_lem.platform.db.repositories.users import admin_email_allowlist
from cqc_lem.platform.db.shared import _FEEDBACK_COLUMNS
from cqc_lem.utilities.logger import log_error

# --- Story bank / fact intake (issue #620) ---
# The user's OWN raw material: the anecdotes, numbers, opinions, wins, mistakes and artifacts a post
# is allowed to cite as a personal specific. `profiles.synthesis` (V48) is the VOICE brief; this is
# the FACT source, and generation may not invent specifics outside it (issue #416).
STORY_BANK_KINDS = ("anecdote", "number", "opinion", "client_win", "mistake", "artifact")
_LEN_STORY_TITLE = 255
def count_story_bank_entries(user_id: int, active_only: bool = True) -> int:
    """How many entries the user has seeded — what the onboarding nudge decides on."""
    try:
        with db_cursor() as cursor:
            sql = "SELECT COUNT(*) FROM story_bank WHERE user_id=%s"
            if active_only:
                sql += " AND active=1"
            cursor.execute(sql, (user_id,))
            row = cursor.fetchone()
            return int(row[0]) if row else 0
    except mysql.connector.Error as err:
        log_error("Could not count story bank entries", exc=err, user_id=user_id)
        return 0
def upsert_story_bank_entries(user_id: int, entries: list) -> bool:
    """Insert new entries and update existing ones (matched on id + user_id). The rotation counters
    belong to generation, so an edit never resets used_count/last_used_at.
    """
    inserts, updates = [], []
    for e in entries or []:
        title = str(e.get("title") or "").strip()[:_LEN_STORY_TITLE]
        body = str(e.get("body") or "").strip()
        if not body:
            continue
        kind = e.get("kind")
        kind = kind if kind in STORY_BANK_KINDS else "anecdote"
        # The whole point is low friction: a user who only types the story gets a title for free.
        title = title or body[:80]
        happened_at = e.get("happened_at") or None
        active = 1 if e.get("active", True) else 0
        entry_id = e.get("id")
        if entry_id:
            updates.append((kind, title, body, happened_at, active, int(entry_id), user_id))
        else:
            inserts.append((user_id, kind, title, body, happened_at, active))
    if not inserts and not updates:
        return True
    try:
        with db_cursor(commit=True) as cursor:
            if inserts:
                cursor.executemany(
                    "INSERT INTO story_bank (user_id, kind, title, body, happened_at, active) "
                    "VALUES (%s,%s,%s,%s,%s,%s)", inserts)
            if updates:
                cursor.executemany(
                    "UPDATE story_bank SET kind=%s, title=%s, body=%s, happened_at=%s, active=%s "
                    "WHERE id=%s AND user_id=%s", updates)
            return True
    except mysql.connector.Error as err:
        log_error("Could not upsert story bank entries", exc=err, user_id=user_id)
        return False
def delete_story_bank_entry(user_id: int, entry_id: int) -> bool:
    """Remove one story-bank entry, scoped to its owner so an id from another account matches nothing.

    True means the DELETE ran, not that a row matched.
    """
    try:
        with db_cursor(commit=True) as cursor:
            cursor.execute("DELETE FROM story_bank WHERE user_id=%s AND id=%s", (user_id, int(entry_id)))
            return True
    except mysql.connector.Error as err:
        log_error("Could not delete story bank entry", exc=err, user_id=user_id)
        return False
def record_story_bank_use(user_id: int, entry_id: int) -> bool:
    """Count one use against an entry so the next post rotates to different raw material."""
    try:
        with db_cursor(commit=True) as cursor:
            cursor.execute(
                "UPDATE story_bank SET used_count = used_count + 1, last_used_at = NOW() "
                "WHERE user_id=%s AND id=%s", (user_id, int(entry_id)))
            return cursor.rowcount > 0
    except mysql.connector.Error as err:
        log_error("Could not record story bank use", exc=err, user_id=user_id)
        return False
def insert_feedback(body: str, user_id: int = None,
                    source: "FeedbackSource" = FeedbackSource.WIDGET,
                    type_hint: str = None, context: dict = None,
                    sentiment: str = None) -> Optional[int]:
    """Persist one piece of user feedback (issue #496). user_id is optional — the widget is offered
    to logged-out visitors too. `context` is the auto-attached client context (route, app version,
    PostHog session id, optional screenshot) and is stored as JSON.
    """
    if not body or not str(body).strip():
        return None
    try:
        with db_cursor(commit=True) as cursor:
            cursor.execute(
                "INSERT INTO feedback (user_id, source, type_hint, body, context_json, sentiment) "
                "VALUES (%s,%s,%s,%s,%s,%s)",
                (user_id, str(source), str(type_hint)[:32] if type_hint else None, str(body),
                 json.dumps(context) if context else None,
                 str(sentiment)[:16] if sentiment else None))
            return cursor.lastrowid
    except mysql.connector.Error as err:
        log_error("Could not insert feedback", exc=err, user_id=user_id)
        return None
def get_feedback_by_id(feedback_id: int) -> Optional[dict]:
    """One feedback row, or None when it does not exist (issue #498)."""
    try:
        with db_cursor(dictionary=True) as cursor:
            cursor.execute(f"SELECT {_FEEDBACK_COLUMNS} FROM feedback WHERE id=%s", (feedback_id,))
            return cursor.fetchone()
    except mysql.connector.Error as err:
        log_error(f"Could not fetch feedback {feedback_id}", exc=err)
        return None
def get_open_feedback_clusters(limit: int = 100) -> list:
    """The open clusters an incoming report can be deduped against (issue #498).

    One row per cluster: the seed's body/embedding (what similarity is measured against), the GitHub
    issue it was filed as, plus `item_count` and `reporter_count` (DISTINCT non-null user_id) — the
    demand signal that decides whether a *feature* cluster is allowed to auto-work.
    """
    try:
        with db_cursor(dictionary=True) as cursor:
            cursor.execute(
                "SELECT s.id AS cluster_id, s.body AS body, s.embedding AS embedding, "
                "       s.github_issue_number AS github_issue_number, s.type_hint AS type_hint, "
                "       COUNT(m.id) AS item_count, "
                "       COUNT(DISTINCT m.user_id) AS reporter_count, "
                "       MAX(m.created_at) AS last_seen_at "
                "FROM feedback s JOIN feedback m ON m.cluster_id = s.cluster_id "
                "WHERE s.cluster_id = s.id AND s.status IN ('clustered','issue_created') "
                "GROUP BY s.id, s.body, s.embedding, s.github_issue_number, s.type_hint "
                "ORDER BY last_seen_at DESC LIMIT %s",
                (int(limit),))
            return cursor.fetchall() or []
    except mysql.connector.Error as err:
        log_error("Could not fetch open feedback clusters", exc=err)
        return []
def count_feedback_filed_by_user(user_id: int, hours: int = 24) -> int:
    """How many of this user's reports reached GitHub in the last `hours` — the abuse guard's counter
    (issue #498). Anonymous feedback (NULL user_id) is not attributable, so it is never counted.
    """
    if user_id is None:
        return 0
    try:
        with db_cursor() as cursor:
            cursor.execute(
                "SELECT COUNT(*) FROM feedback WHERE user_id=%s AND github_issue_number IS NOT NULL "
                "AND created_at >= (NOW() - INTERVAL %s HOUR)",
                (user_id, int(hours)))
            row = cursor.fetchone()
            return int(row[0]) if row and row[0] is not None else 0
    except mysql.connector.Error as err:
        log_error("Could not count filed feedback for user", exc=err, user_id=user_id)
        return 0
def update_feedback_triage(feedback_id: int,
                           status: Optional[Union["FeedbackStatus", str]] = None,
                           cluster_id: int = None, github_issue_number: int = None,
                           embedding: list = None, sentiment: str = None) -> bool:
    """Stamp the auto-triage result back onto a feedback row (issue #498). Only the arguments you
    pass are written, so the filer can save an embedding on one pass and the cluster/issue on the
    next without clobbering anything.

    `status` is checked against `FeedbackStatus` BEFORE the write (issue #668): the column is a
    MySQL ENUM, so an out-of-vocabulary value used to surface as an opaque `1265 Data truncated for
    column 'status'` AND roll back the cluster/issue/embedding travelling in the same UPDATE.
    """
    updates: list = []
    params: list = []
    if status is not None:
        try:
            status = FeedbackStatus(str(status).strip().lower())
        except ValueError as err:
            # exc= is what turns this into a grouped PostHog $exception (issue #648) — the 1265 it
            # replaces was one, so without it the refusal would page nobody and only land in Logs.
            log_error(f"Refusing to write unknown feedback status {str(status)!r} for feedback "
                      f"{feedback_id} — expected one of "
                      f"{', '.join(s.value for s in FeedbackStatus)}", exc=err)
            return False
        updates.append("status=%s")
        params.append(str(status))
    if cluster_id is not None:
        updates.append("cluster_id=%s")
        params.append(int(cluster_id))
    if github_issue_number is not None:
        updates.append("github_issue_number=%s")
        params.append(int(github_issue_number))
    if embedding is not None:
        updates.append("embedding=%s")
        params.append(json.dumps(embedding))
    if sentiment is not None:
        updates.append("sentiment=%s")
        params.append(str(sentiment)[:16])
    if not updates:
        return False
    try:
        with db_cursor(commit=True) as cursor:
            params.append(feedback_id)
            cursor.execute(f"UPDATE feedback SET {', '.join(updates)} WHERE id=%s", tuple(params))
            return cursor.rowcount > 0
    except mysql.connector.Error as err:
        columns = ", ".join(u.split("=")[0] for u in updates)
        log_error(f"Could not update feedback triage for {feedback_id} (columns: {columns})",
                  exc=err)
        return False
def record_feedback_review(feedback_id: int, reviewer_user_id: int,
                           status: Optional[Union["FeedbackStatus", str]] = None,
                           reviewed_at: Optional[datetime] = None) -> bool:
    """Stamp who reviewed a feedback row and when, optionally updating its status (issue #793).

    Status is validated the same way as `update_feedback_triage` so a typo can never corrupt the
    ENUM column.
    """
    if feedback_id is None or reviewer_user_id is None:
        return False
    updates: list = ["reviewed_by=%s", "reviewed_at=%s"]
    params: list = [int(reviewer_user_id),
                    (reviewed_at or datetime.now(timezone.utc)).replace(tzinfo=None)]
    if status is not None:
        try:
            status = FeedbackStatus(str(status).strip().lower())
        except ValueError as err:
            log_error(f"Refusing to write unknown feedback status {str(status)!r} for feedback "
                      f"{feedback_id} — expected one of "
                      f"{', '.join(s.value for s in FeedbackStatus)}", exc=err)
            return False
        updates.append("status=%s")
        params.append(str(status))
    params.append(int(feedback_id))
    try:
        with db_cursor(commit=True) as cursor:
            cursor.execute(f"UPDATE feedback SET {', '.join(updates)} WHERE id=%s", tuple(params))
            return cursor.rowcount > 0
    except mysql.connector.Error as err:
        log_error(f"Could not record feedback review for {feedback_id}", exc=err,
                  user_id=reviewer_user_id)
        return False
# --- NPS/CSAT + review surveys (issue #501) -----------------------------------------
def get_latest_feedback_at(user_id: int, source: "FeedbackSource") -> Optional[datetime]:
    """When this user last answered a survey of the given source, or None if they never have.
    Drives both "don't ask again" suppression and the review gate on the extended trial.
    """
    try:
        with db_cursor() as cursor:
            cursor.execute(
                "SELECT MAX(created_at) FROM feedback WHERE user_id = %s AND source = %s",
                (user_id, str(source)))
            row = cursor.fetchone()
            return row[0] if row else None
    except mysql.connector.Error as err:
        log_error(f"Could not get latest {source} feedback for user_id {user_id}", exc=err)
        return None
def has_review_feedback(user_id: int) -> bool:
    """The extended-trial gate (issue #499 consumes this): True once the user has submitted a
    review. Fails CLOSED — a DB error never hands out a trial extension.
    """
    try:
        with db_cursor() as cursor:
            cursor.execute("SELECT 1 FROM feedback WHERE user_id = %s AND source = %s LIMIT 1",
                           (user_id, str(FeedbackSource.REVIEW)))
            return cursor.fetchone() is not None
    except mysql.connector.Error as err:
        log_error(f"Could not check review feedback for user_id {user_id}", exc=err)
        return False
def get_survey_prompts_sent(user_id: int) -> dict:
    """survey_key -> sent_at for every survey prompt already shown/emailed to this user."""
    try:
        with db_cursor() as cursor:
            cursor.execute("SELECT survey_key, sent_at FROM survey_prompts WHERE user_id = %s",
                           (user_id,))
            return {row[0]: row[1] for row in cursor.fetchall()}
    except mysql.connector.Error as err:
        log_error(f"Could not get survey prompts for user_id {user_id}", exc=err)
        return {}
def record_survey_prompt(user_id: int, survey_key: str) -> bool:
    """Record that a survey was asked. Returns False when it was already asked (the PK makes each
    survey one-shot per user, whether it went out in-app or by email).
    """
    try:
        with db_cursor(commit=True) as cursor:
            cursor.execute("INSERT IGNORE INTO survey_prompts (user_id, survey_key) VALUES (%s, %s)",
                           (user_id, str(survey_key)[:32]))
            return cursor.rowcount > 0
    except mysql.connector.Error as err:
        log_error(f"Could not record survey prompt {survey_key} for user_id {user_id}", exc=err)
        return False
# --- Shipped-fix changelog + reporter notification (issue #502) ---------------------
def get_feedback_reporters_for_issue(github_issue_number: int) -> list:
    """Every identified user who reported the problem a GitHub issue was filed for — the
    feedback→issue→cluster mapping the "you asked, we shipped" notice is addressed to.

    Matches BOTH the rows stamped with the issue directly (the seed and every deduped report) and
    any row that only carries the seed's `cluster_id`, so a report attached by the nightly recluster
    pass before the issue number propagated is still counted. Anonymous rows have no one to tell.
    """
    if not github_issue_number:
        return []
    try:
        with db_cursor() as cursor:
            cursor.execute(
                "SELECT DISTINCT f.user_id FROM feedback f "
                "LEFT JOIN feedback s ON s.id = f.cluster_id "
                "WHERE f.user_id IS NOT NULL "
                "  AND (f.github_issue_number = %s OR s.github_issue_number = %s)",
                (int(github_issue_number), int(github_issue_number)))
            return [row[0] for row in cursor.fetchall()]
    except mysql.connector.Error as err:
        log_error(f"Could not get feedback reporters for issue {github_issue_number}", exc=err)
        return []
def mark_feedback_resolved_for_issue(github_issue_number: int) -> int:
    """Close the loop on a shipped cluster: every still-open report behind this issue becomes
    `resolved`. Dismissed rows are left alone (they were never part of the fix). Returns how many
    rows moved.

    Uses the SAME self-join as `get_feedback_reporters_for_issue`, so a report attached to the seed
    by `cluster_id` before the issue number propagated is resolved too — otherwise the users we
    notify and the rows we close would drift apart.
    """
    if not github_issue_number:
        return 0
    try:
        with db_cursor(commit=True) as cursor:
            cursor.execute(
                "UPDATE feedback f "
                "LEFT JOIN feedback s ON s.id = f.cluster_id "
                "SET f.status = %s "
                "WHERE (f.github_issue_number = %s OR s.github_issue_number = %s) "
                "  AND f.status NOT IN (%s, %s)",
                (str(FeedbackStatus.RESOLVED), int(github_issue_number), int(github_issue_number),
                 str(FeedbackStatus.RESOLVED), str(FeedbackStatus.DISMISSED)))
            return cursor.rowcount or 0
    except mysql.connector.Error as err:
        log_error(f"Could not resolve feedback for issue {github_issue_number}", exc=err)
        return 0
# --- Public FAQ (issue #506) --------------------------------------------------------
def get_published_faq_entries(limit: int = 50) -> list:
    """The published FAQ shown on the landing page, in display order. Only PUBLISHED rows leave the
    database — drafts written by the auto-FAQ pass stay unpublished until reviewed.
    """
    # Connect inside the try: an unreachable database must degrade to an empty FAQ, never bubble an
    # exception up into the logged-out landing page.
    connection = None
    cursor = None
    try:
        connection = _connection.get_db_connection()
        cursor = connection.cursor(dictionary=True)
        cursor.execute(
            "SELECT id, question, answer, cluster_id, updated_at FROM faq_entries "
            "WHERE status = %s ORDER BY sort_order ASC, id ASC LIMIT %s",
            (FaqStatus.PUBLISHED.value, int(limit)))
        return cursor.fetchall() or []
    except mysql.connector.Error as err:
        log_error("Could not get published FAQ entries", exc=err)
        return []
    finally:
        if cursor is not None:
            cursor.close()
        if connection is not None:
            connection.close()
# --- Auto-FAQ maintenance (issue #507) ----------------------------------------------
_FAQ_COLUMNS = "id, question, answer, cluster_id, status, sort_order, created_at, updated_at"
def get_faq_entries(statuses: tuple = (FaqStatus.PUBLISHED, FaqStatus.DRAFT),
                    limit: int = 200) -> list:
    """Every FAQ entry the auto-FAQ pass matches an incoming question against (issue #507) — drafts
    included, so a question that already has a proposed answer is never answered twice.
    """
    wanted = [str(s) for s in (statuses or ()) if str(s) in tuple(FaqStatus)]
    if not wanted:
        return []
    try:
        with db_cursor(dictionary=True) as cursor:
            cursor.execute(
                f"SELECT {_FAQ_COLUMNS} FROM faq_entries "
                f"WHERE status IN ({','.join(['%s'] * len(wanted))}) "
                "ORDER BY sort_order ASC, id ASC LIMIT %s",
                (*wanted, int(limit)))
            return cursor.fetchall() or []
    except mysql.connector.Error as err:
        log_error("Could not get FAQ entries", exc=err)
        return []
def get_faq_entry_by_cluster(cluster_id: int) -> Optional[dict]:
    """The FAQ entry a feedback cluster already produced, or None (issue #507)."""
    if cluster_id is None:
        return None
    try:
        with db_cursor(dictionary=True) as cursor:
            cursor.execute(f"SELECT {_FAQ_COLUMNS} FROM faq_entries WHERE cluster_id=%s "
                           "ORDER BY id ASC LIMIT 1", (int(cluster_id),))
            return cursor.fetchone()
    except mysql.connector.Error as err:
        log_error(f"Could not get FAQ entry for cluster {cluster_id}", exc=err)
        return None
def upsert_faq_entry(question: str, answer: str, cluster_id: int = None,
                     status: "FaqStatus" = FaqStatus.DRAFT,
                     sort_order: int = None) -> Optional[int]:
    """Write one FAQ answer, keyed on the question text (issue #507). Returns the entry id — the
    `id=LAST_INSERT_ID(id)` trick makes that the EXISTING id when the question is already there, so
    the caller can version the revision instead of creating a duplicate entry.

    `sort_order` is only written on insert: re-answering a question must never re-order the page.
    """
    if not question or not str(question).strip() or not answer or not str(answer).strip():
        return None
    try:
        with db_cursor(commit=True) as cursor:
            cursor.execute(
                "INSERT INTO faq_entries (question, answer, cluster_id, status, sort_order) "
                "VALUES (%s,%s,%s,%s,%s) "
                "ON DUPLICATE KEY UPDATE id=LAST_INSERT_ID(id), answer=VALUES(answer), "
                "cluster_id=COALESCE(VALUES(cluster_id), cluster_id), status=VALUES(status)",
                (str(question)[:512], str(answer), int(cluster_id) if cluster_id is not None else None,
                 str(status), int(sort_order) if sort_order is not None else 0))
            return cursor.lastrowid
    except mysql.connector.Error as err:
        log_error("Could not upsert FAQ entry", exc=err)
        return None
def record_faq_entry_version(faq_entry_id: int, question: str, answer: str,
                             status: "FaqStatus" = FaqStatus.DRAFT,
                             source: str = 'auto') -> Optional[int]:
    """Append the state an FAQ entry was just put into (issue #507). History is append-only, so any
    earlier answer stays revertible after the auto-FAQ pass rewrites it.
    """
    if faq_entry_id is None or not answer or not str(answer).strip():
        return None
    try:
        with db_cursor(commit=True) as cursor:
            cursor.execute(
                "INSERT INTO faq_entry_versions (faq_entry_id, question, answer, status, source) "
                "VALUES (%s,%s,%s,%s,%s)",
                (int(faq_entry_id), str(question)[:512], str(answer), str(status), str(source)[:32]))
            return cursor.lastrowid
    except mysql.connector.Error as err:
        log_error(f"Could not record FAQ version for entry {faq_entry_id}", exc=err)
        return None
def get_faq_entry_versions(faq_entry_id: int, limit: int = 20) -> list:
    """An FAQ entry's answer history, newest first (issue #507)."""
    if faq_entry_id is None:
        return []
    try:
        with db_cursor(dictionary=True) as cursor:
            cursor.execute(
                "SELECT id, faq_entry_id, question, answer, status, source, created_at "
                "FROM faq_entry_versions WHERE faq_entry_id=%s ORDER BY id DESC LIMIT %s",
                (int(faq_entry_id), int(limit)))
            return cursor.fetchall() or []
    except mysql.connector.Error as err:
        log_error(f"Could not get FAQ versions for entry {faq_entry_id}", exc=err)
        return []
def apply_faq_entry_version(faq_entry_id: int, version_id: int) -> Optional[dict]:
    """Re-apply a stored version's copy and status onto its entry (issue #507) — the revert half of
    versioned answers. Returns the applied version, or None when it doesn't belong to that entry.

    Recording the revert itself as a NEW version is the caller's job, so history stays append-only.
    """
    if faq_entry_id is None or version_id is None:
        return None
    connection = _connection.get_db_connection()
    cursor = connection.cursor(dictionary=True)
    try:
        cursor.execute(
            "SELECT id, faq_entry_id, question, answer, status, source, created_at "
            "FROM faq_entry_versions WHERE id=%s AND faq_entry_id=%s",
            (int(version_id), int(faq_entry_id)))
        version = cursor.fetchone()
        if not version:
            return None
        cursor.execute("UPDATE faq_entries SET question=%s, answer=%s, status=%s WHERE id=%s",
                       (version["question"], version["answer"], str(version["status"]),
                        int(faq_entry_id)))
        connection.commit()
        return version
    except mysql.connector.Error as err:
        log_error(f"Could not revert FAQ entry {faq_entry_id} to version {version_id}", exc=err)
        return None
    finally:
        cursor.close()
        connection.close()
def get_faq_candidate_feedback(limit: int = 50) -> list:
    """Feedback the auto-FAQ pass may answer (issue #507): rows the auto-filer already looked at
    (`triaged`) and did NOT turn into work (still unclustered) — the FAQ-routed questions and public
    review free-text. Rows still in `new` are deliberately excluded: the filer classifies first, so
    the FAQ pass can never claim a report that was going to become an issue.
    """
    try:
        with db_cursor(dictionary=True) as cursor:
            cursor.execute(
                f"SELECT {_FEEDBACK_COLUMNS} FROM feedback "
                "WHERE status=%s AND cluster_id IS NULL ORDER BY created_at ASC, id ASC LIMIT %s",
                (str(FeedbackStatus.TRIAGED), int(limit)))
            return cursor.fetchall() or []
    except mysql.connector.Error as err:
        log_error("Could not fetch FAQ candidate feedback", exc=err)
        return []
def get_latest_review_feedback_id(user_id: int) -> Optional[int]:
    """The most recent `feedback` row this user filed with source='review' — the gate the
    early-adopter extension is traded for. None when they haven't left a review yet.
    """
    try:
        with db_cursor() as cursor:
            cursor.execute(
                "SELECT id FROM feedback WHERE user_id=%s AND source=%s ORDER BY created_at DESC, id DESC LIMIT 1",
                (user_id, str(FeedbackSource.REVIEW)),
            )
            row = cursor.fetchone()
            return int(row[0]) if row else None
    except mysql.connector.Error as err:
        log_error("Could not look up review feedback", exc=err, user_id=user_id)
        return None


_STORY_BANK_COLS = ("id", "kind", "title", "body", "happened_at", "used_count", "last_used_at",
                    "active")
def _clean_story_row(row: dict) -> dict:
    row["active"] = bool(row.get("active"))
    row["used_count"] = int(row.get("used_count") or 0)
    return row
def get_story_bank_entries(user_id: int, active_only: bool = False) -> list:
    """The user's story bank, least-recently-used first — the rotation order the selector consumes
    directly (never-used entries sort ahead of used ones, oldest use next).
    """
    try:
        with db_cursor(dictionary=True) as cursor:
            sql = f"SELECT {', '.join(_STORY_BANK_COLS)} FROM story_bank WHERE user_id=%s"
            if active_only:
                sql += " AND active=1"
            sql += " ORDER BY used_count ASC, last_used_at IS NOT NULL, last_used_at ASC, id ASC"
            cursor.execute(sql, (user_id,))
            return [_clean_story_row(r) for r in (cursor.fetchall() or [])]
    except mysql.connector.Error as err:
        log_error("Could not list story bank entries", exc=err, user_id=user_id)
        return []
def _prefixed_feedback_columns(alias: str = "f") -> str:
    return ", ".join(f"{alias}.{c.strip()}" for c in _FEEDBACK_COLUMNS.split(","))


# What "a seeded bank" means — the onboarding nudge and the SPA both aim the user at this many.
STORY_BANK_TARGET_ENTRIES = 5
def _admin_reporter_join(alias: str = "f") -> tuple:
    """LEFT JOIN + params that mark whether a feedback row was submitted by an admin (#793).

    LEFT so it can express both halves: `au.id IS NOT NULL` is admin, `au.id IS NULL` is pending.
    """
    allow = sorted(admin_email_allowlist())
    email_clause = f" OR LOWER(au.email) IN ({','.join(['%s'] * len(allow))})" if allow else ""
    join = (f"LEFT JOIN users au ON au.id = {alias}.user_id "
            f"AND (au.is_admin = 1{email_clause})")
    return join, tuple(allow)
def get_unprocessed_feedback(limit: int = 25, statuses: tuple = (FeedbackStatus.NEW,),
                             admin_only: bool = False) -> list:
    """Captured-but-unclustered feedback, oldest first so the queue drains FIFO (issue #498).

    Defaults to `new` only — the auto-filer must not re-classify (and re-pay for) rows it already
    parked in `triaged`. The nightly reclustering pass widens `statuses` to reconsider those.

    `admin_only` (issue #793) restricts the result to reports from admin users. It filters in SQL,
    NOT in the caller's loop: non-admin rows keep their `new`/NULL-cluster shape forever while they
    wait on the panel, so a caller-side skip would let `limit` fill with the same parked rows every
    pass and admin feedback would never be reached again.
    """
    wanted = [str(s) for s in (statuses or ()) if str(s) in tuple(FeedbackStatus)]
    if not wanted:
        return []
    join, join_params = _admin_reporter_join() if admin_only else ("", ())
    admin_filter = "AND au.id IS NOT NULL " if admin_only else ""
    try:
        with db_cursor(dictionary=True) as cursor:
            cursor.execute(
                f"SELECT {_prefixed_feedback_columns()} FROM feedback f {join} "
                f"WHERE f.status IN ({','.join(['%s'] * len(wanted))}) AND f.cluster_id IS NULL "
                f"{admin_filter}"
                "ORDER BY f.created_at ASC, f.id ASC LIMIT %s",
                (*join_params, *wanted, int(limit)))
            return cursor.fetchall() or []
    except mysql.connector.Error as err:
        log_error("Could not fetch unprocessed feedback", exc=err)
        return []
def count_pending_admin_review(statuses: tuple = (FeedbackStatus.NEW,)) -> int:
    """How many un-clustered reports are waiting on an admin decision (issue #793).

    The inverse of `get_unprocessed_feedback(admin_only=True)`: everything the auto-filer skipped.
    Reported by `process_new_feedback` so a silent backlog is visible without opening the panel.
    """
    wanted = [str(s) for s in (statuses or ()) if str(s) in tuple(FeedbackStatus)]
    if not wanted:
        return 0
    join, join_params = _admin_reporter_join()
    try:
        with db_cursor() as cursor:
            cursor.execute(
                f"SELECT COUNT(*) FROM feedback f {join} "
                f"WHERE f.status IN ({','.join(['%s'] * len(wanted))}) AND f.cluster_id IS NULL "
                "AND au.id IS NULL",
                (*join_params, *wanted))
            row = cursor.fetchone()
            return int(row[0]) if row else 0
    except mysql.connector.Error as err:
        log_error("Could not count feedback pending admin review", exc=err)
        return 0
def get_feedback_list(status: Optional[Union["FeedbackStatus", str]] = None,
                      source: Optional[Union["FeedbackSource", str]] = None,
                      limit: int = 50, offset: int = 0) -> list:
    """All feedback rows, newest first, with the submitter's email and admin flag (issue #793).

    Optional status/source filters are validated against the enum vocabularies before they reach
    the query, so a bad value returns an empty list instead of a MySQL 1265.

    `embedding` is deliberately NOT selected — the panel never shows it, and a page of 50 rows would
    drag 50 full vectors out of MySQL to be thrown away. `is_admin` answers the same question the
    auto-filer's join does, so it honours ADMIN_USER_EMAILS too: an allowlisted reporter's feedback
    IS auto-filed, and the panel must not label it as awaiting review.
    """
    filters: list = []
    params: list = []
    if status is not None:
        try:
            status = FeedbackStatus(str(status).strip().lower())
        except ValueError:
            return []
        filters.append("f.status = %s")
        params.append(str(status))
    if source is not None:
        try:
            source = FeedbackSource(str(source).strip().lower())
        except ValueError:
            return []
        filters.append("f.source = %s")
        params.append(str(source))

    where = f"WHERE {' AND '.join(filters)}" if filters else ""
    sql = (
        f"SELECT f.id, f.user_id, f.source, f.type_hint, f.body, f.context_json, "
        f"f.cluster_id, f.github_issue_number, f.status, f.sentiment, "
        f"f.reviewed_by, f.reviewed_at, f.created_at, u.email, u.is_admin "
        f"FROM feedback f LEFT JOIN users u ON u.id = f.user_id "
        f"{where} ORDER BY f.created_at DESC LIMIT %s OFFSET %s"
    )
    try:
        with db_cursor(dictionary=True) as cursor:
            cursor.execute(sql, (*params, int(limit), int(offset)))
            rows = cursor.fetchall() or []
            allow = admin_email_allowlist()
            for row in rows:
                if allow and not row.get("is_admin") and \
                        (row.get("email") or "").strip().lower() in allow:
                    row["is_admin"] = 1
            return rows
    except mysql.connector.Error as err:
        log_error("Could not list feedback for admin panel", exc=err)
        return []
