"""Every SQL statement LEM runs against the groups tables.

Split out of `cqc_lem.utilities.db` (issue #1154). The fail-soft reader contract and the
secret-sealing rules described there apply here unchanged; `cqc_lem.utilities.db`
re-exports every name below, so existing importers and patch targets keep resolving.
"""

from typing import Optional

import mysql.connector

from cqc_lem.platform.db.connection import db_cursor
from cqc_lem.platform.db.enums import GroupPostDraftStatus, GroupPostMediaType
from cqc_lem.utilities.logger import log_error, log_info

# "the caller said nothing about the media", which is not the same answer as "the caller said there
# is no media" — see `update_group_post_draft`.
_UNSET = object()


def upsert_user_group(user_id: int, group_id: str, group_name: str = None) -> bool:
    """Record a joined group (new groups default to enabled=1). Refreshes name + last_synced_at
    without clobbering the user's enabled choice on an existing row.
    """
    if not group_id:
        return False
    try:
        with db_cursor(commit=True) as cursor:
            cursor.execute(
                "INSERT INTO user_groups (user_id, group_id, group_name, enabled, last_synced_at) "
                "VALUES (%s,%s,%s,1,NOW()) ON DUPLICATE KEY UPDATE "
                "group_name=COALESCE(VALUES(group_name), group_name), last_synced_at=NOW()",
                (user_id, str(group_id), group_name))
            return True
    except mysql.connector.Error as err:
        log_error("Could not upsert group", exc=err, user_id=user_id)
        return False
def get_user_groups(user_id: int) -> list:
    """The user's LinkedIn groups for the Account UI, JSON-safe (flags as bools, timestamps as ISO strings).

    `enabled` and `post_enabled` are independent switches on purpose — being in a group is not permission
    to publish into it. [] on a read error.
    """
    try:
        with db_cursor(dictionary=True) as cursor:
            cursor.execute(
                "SELECT group_id, group_name, enabled, post_enabled, last_posted_at "
                "FROM user_groups WHERE user_id=%s ORDER BY group_name",
                (user_id,))
            rows = cursor.fetchall() or []
            for r in rows:
                r["enabled"] = bool(r.get("enabled"))
                r["post_enabled"] = bool(r.get("post_enabled"))
                posted = r.get("last_posted_at")
                r["last_posted_at"] = posted.isoformat() if hasattr(posted, "isoformat") else posted
            return rows
    except mysql.connector.Error as err:
        log_error("Could not list groups", exc=err, user_id=user_id)
        return []
def get_enabled_group_ids(user_id: int) -> list:
    """Group ids the user has enabled for engagement.

    Swallows the error without logging and returns [], so a database blip reads as "no groups" and skips
    the group pass instead of failing the run.
    """
    try:
        with db_cursor() as cursor:
            cursor.execute("SELECT group_id FROM user_groups WHERE user_id=%s AND enabled=1", (user_id,))
            return [r[0] for r in cursor.fetchall()]
    except mysql.connector.Error:
        return []
def get_next_group_for_post(user_id: int) -> Optional[dict]:
    """The ONE place "which group does the next group post go to" is decided (issue #769): the
    least-recently-TRIED group the user has opted into for POSTING. `post_enabled` is independent
    of `enabled` (which only governs commenting), so a group can take posts without being commented
    in and vice versa. Never tried = sorts first. None when no group is opted in.

    Ordering reads `last_post_run_at` (every run that reached the group), NOT `last_posted_at`
    (successful posts only) — a group where members cannot post never stamps the latter, so ordering
    on it left that group "next" every week forever and starved the rest (issue #858). The COALESCE
    covers any row stamped before `last_post_run_at` existed.
    """
    try:
        with db_cursor(dictionary=True) as cursor:
            cursor.execute(
                "SELECT group_id, group_name FROM user_groups "
                "WHERE user_id=%s AND post_enabled=1 "
                "ORDER BY COALESCE(last_post_run_at, last_posted_at) IS NULL DESC, "
                "         COALESCE(last_post_run_at, last_posted_at) ASC, group_name ASC LIMIT 1",
                (user_id,))
            row = cursor.fetchone()
            return dict(row) if row else None
    except mysql.connector.Error as err:
        log_error("Could not resolve next post group", exc=err, user_id=user_id)
        return None
def record_group_post(user_id: int, group_id: str) -> bool:
    """Stamp a group as just-posted-in so the rotation moves on to the next one. A successful post
    is also a run, so both columns advance together.
    """
    try:
        with db_cursor(commit=True) as cursor:
            cursor.execute("UPDATE user_groups SET last_posted_at=NOW(), last_post_run_at=NOW() "
                           "WHERE user_id=%s AND group_id=%s",
                           (user_id, str(group_id)))
            return True
    except mysql.connector.Error as err:
        log_error("Could not record group post", exc=err, user_id=user_id)
        return False
def record_group_post_run(user_id: int, group_id: str) -> bool:
    """Stamp a group as just-TRIED (issue #858) — the rotation moves on, but `last_posted_at` is
    left alone because nothing was published. Called only when the run reached the group and the
    group itself turned out to be unpostable (no share box / editor / Post button — admin-only or
    announcement groups). A run that never reached the group stamps neither column, so a transient
    session failure still leaves that group next in line.
    """
    try:
        with db_cursor(commit=True) as cursor:
            cursor.execute("UPDATE user_groups SET last_post_run_at=NOW() WHERE user_id=%s AND group_id=%s",
                           (user_id, str(group_id)))
            return True
    except mysql.connector.Error as err:
        # A lost stamp is exactly the starvation this function exists to prevent — the group stays
        # least-recently-tried and is "next" again next week — and the caller has nothing to do
        # about it, so it has to be visible on its own (ERROR, not the myprint shim: prod forwards
        # WARNING and above to PostHog).
        log_error("Could not record group post run", exc=err, user_id=user_id,
                  task_name="record_group_post_run")
        return False
def set_groups_enabled(user_id: int, group_states: dict) -> bool:
    """Bulk-update per-group flags. Each value is either a bare bool (engagement only — the shape
    the pre-#769 SPA bundle still sends) or {"enabled": bool, "post_enabled": bool}; only the keys
    present are written, so a partial payload never silently resets the other flag.
    """
    try:
        with db_cursor(commit=True) as cursor:
            for gid, state in group_states.items():
                flags = state if isinstance(state, dict) else {"enabled": state}
                updates = [(col, 1 if flags[col] else 0) for col in ("enabled", "post_enabled") if col in flags]
                if not updates:
                    continue
                cursor.execute(
                    f"UPDATE user_groups SET {', '.join(f'{c}=%s' for c, _ in updates)} "
                    "WHERE user_id=%s AND group_id=%s",
                    (*(v for _, v in updates), user_id, str(gid)))
            return True
    except mysql.connector.Error as err:
        log_error("Could not update group states", exc=err, user_id=user_id)
        return False
def disable_user_groups(user_id: int, group_ids: list, reason: str = "") -> bool:
    """Switch engagement off for groups a sync proved are not memberships (issue #1487).

    Deliberately NOT `set_groups_enabled`, which is the SPA's bulk writer and can flip a flag either
    way: this one only ever writes `enabled=0`, never touches `post_enabled`, and never DELETEs — the
    row stays in `user_groups`, so `get_user_groups` keeps listing it and the user can turn it back on
    from the Account UI. It logs what it switched off with the user and the ids, because a group going
    quiet on its own is otherwise invisible until someone notices the silence.
    """
    ids = [str(g) for g in (group_ids or []) if str(g or "").strip()]
    if not ids:
        return False
    try:
        with db_cursor(commit=True) as cursor:
            cursor.execute(
                f"UPDATE user_groups SET enabled=0 WHERE user_id=%s AND group_id IN "
                f"({','.join(['%s'] * len(ids))})",
                (user_id, *ids))
        log_info("Disabled group(s) the live directory did not confirm as memberships",
                 user_id=user_id, group_ids=",".join(ids), reason=reason or "unspecified",
                 task_name="auto_sync_user_groups")
        return True
    except mysql.connector.Error as err:
        log_error("Could not disable groups", exc=err, user_id=user_id,
                  task_name="auto_sync_user_groups")
        return False
def get_post_enabled_group_ids(user_id: int) -> Optional[list]:
    """The groups the user has opted into for POSTING. Separate from get_enabled_group_ids, which
    reads the independent commenting flag.

    None (never []) when the read FAILED, so a caller can tell "opted out of every group" from "we
    could not tell": the weekly publish run cancels a reviewed draft on the former, and a read error
    that answered [] would silently cancel every user's approved group post (issue #932).
    """
    try:
        with db_cursor() as cursor:
            cursor.execute("SELECT group_id FROM user_groups WHERE user_id=%s AND post_enabled=1", (user_id,))
            return [r[0] for r in cursor.fetchall()]
    except mysql.connector.Error as err:
        log_error("Could not list post-enabled groups", exc=err, user_id=user_id)
        return None
def create_group_post_draft(user_id: int, group_id: str, content: str,
                            group_name: str = None) -> Optional[int]:
    """Store the coming week's group post for review (issue #932). Returns the new draft id."""
    if not group_id or not (content or "").strip():
        return None
    try:
        with db_cursor(commit=True) as cursor:
            cursor.execute(
                "INSERT INTO group_post_drafts (user_id, group_id, group_name, content, status) "
                "VALUES (%s,%s,%s,%s,%s)",
                (user_id, str(group_id), group_name, content.strip(), str(GroupPostDraftStatus.READY)))
            return cursor.lastrowid
    except mysql.connector.Error as err:
        log_error("Could not create group post draft", exc=err, user_id=user_id)
        return None
def update_group_post_draft(draft_id: int, content: str = None,
                            status: "GroupPostDraftStatus" = None,
                            media_url: "str | None" = _UNSET,
                            media_type: "GroupPostMediaType | None" = None) -> bool:
    """Save the user's revision, move the draft's status, and/or attach or clear its media.

    `published_at` is stamped by the status change itself, so the publish run can never claim a ship
    time without the status.

    `media_url` is three-valued on purpose (issue #1224): omitted leaves the attachment alone, a URL
    attaches it, and an explicit `None` detaches it — a caller saving a text edit must not silently
    drop the image the author attached a minute earlier. `media_type` always travels with the URL,
    so a row can never say "there is media" without saying what it is.
    """
    fields, params = [], []
    if content is not None:
        fields.append("content = %s")
        params.append(content.strip())
    if status is not None:
        fields.append("status = %s")
        params.append(str(status))
        if status == GroupPostDraftStatus.PUBLISHED:
            fields.append("published_at = NOW()")
    if media_url is not _UNSET:
        fields.append("media_url = %s")
        params.append(media_url or None)
        fields.append("media_type = %s")
        params.append(str(media_type) if (media_url and media_type) else None)
    if not fields:
        return False
    params.append(draft_id)
    try:
        with db_cursor(commit=True) as cursor:
            cursor.execute(f"UPDATE group_post_drafts SET {', '.join(fields)} WHERE id = %s", tuple(params))
            return True
    except mysql.connector.Error as err:
        log_error("Could not update group post draft", exc=err, task_name="update_group_post_draft")
        return False
