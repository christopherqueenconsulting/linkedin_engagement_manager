"""The settings tools: `lem_update_settings` and `lem_undo_settings_change` (epic #2797, #2800).

Unlike every queue write in `tools_write.py`, these change something that takes effect at once, with
no approval step, so everything about them is narrowed on purpose:

- **An allowlist, in code** (`settings_policy.py`). A field is group 1 (applied here) or it is
  refused with `isError` naming it: group 3 (gate-loosening), a daily cap, a never-field or an
  unknown name. The write routes re-check the same allowlist on the request body for an MCP seat
  (`routers/user._mcp_settings_guard`), so this module cannot widen what a token may change.
- **Merge, never replace.** `PUT /user/engagement-preferences` fills every unsent field with a
  Pydantic default. The tool sends ONLY the keys that actually change, and the route (for an MCP
  seat) writes only the keys it was sent, over the saved row. So a one-field change leaves every
  other stored value as it was.
- **The SPA's own validators.** Values are checked with the route's request models before anything
  is written, and what the route stores is read back for the diff, so a clamp or a tidy shows up as
  the `after` value rather than a surprise.
- **Undo.** Each change that landed stores its before and after values in Redis for seven days under
  an `undo_id`. Undo refuses, naming the fields, if any of them was changed again since.
- **No values in the audit.** Free text is shown to the agent in the diff (it is the user's own
  setting), but the `mcp_settings` audit row carries field names only, and nothing here logs a value.

Both tools run through `writes.run_write` (flag, dedupe, write budgets, audit), behind the
`mcp-settings-writes` flag on top of `mcp-server-writes`.
"""

import json
import secrets
from typing import Any, Optional

from fastapi.concurrency import run_in_threadpool
from pydantic import Field, ValidationError, model_validator

from cqc_lem.api.mcp import settings_policy as policy, writes
from cqc_lem.api.mcp.loopback import (
    INVALID_ARGS,
    REFUSED,
    Loopback,
    ToolFailure,
)
from cqc_lem.api.mcp.tools import BOTH_SCOPES, ToolSpec, _spa_url
from cqc_lem.api.mcp.tools_write import WriteArgs
from cqc_lem.utilities.db import AuthAuditEvent
from cqc_lem.utilities.flags import MCP_SETTINGS_WRITES
from cqc_lem.utilities.logger import log_debug, log_warning

PUT = "PUT"
UNDO_TTL_SECONDS = 7 * 24 * 3600
_MAX_CHANGES_BYTES = 32 * 1024
_UNDO_ID = r"^[A-Za-z0-9]{8,64}$"

_PREFS_PATH = "/user/engagement-preferences"
# Where each resource is READ, and where it is WRITTEN. The ICP is read from the preferences row
# (its own GET also builds a draft from the user's roster and leads, which a tool never wants).
_READ_PATHS = {policy.PREFS: _PREFS_PATH, policy.ICP: _PREFS_PATH,
               policy.LEAD_MAGNET: "/user/lead-magnet", policy.NEWSLETTER: "/user/newsletter-settings",
               policy.TIMEZONE: "/user/timezone"}
_WRITE_PATHS = {policy.PREFS: _PREFS_PATH, policy.ICP: "/user/icp-definition",
                policy.LEAD_MAGNET: "/user/lead-magnet",
                policy.NEWSLETTER: "/user/newsletter-settings", policy.TIMEZONE: "/user/timezone"}
_SETTINGS_CALLS = (("GET", "/user/timezone"), *((PUT, path) for path in sorted(set(_WRITE_PATHS.values()))))

APPLIED_NOTE = ("Applied now: settings take effect immediately and need no approval. Tell the user "
                "exactly what changed; they can undo it with lem_undo_settings_change.")


class UpdateSettingsArgs(WriteArgs):
    """A partial settings change: only the fields named here are touched."""

    changes: dict[str, Any] = Field(..., min_length=1, max_length=40, description=(
        "Map of setting name to its new value; every setting you do not name keeps its value. "
        "Allowed: " + ", ".join(sorted(policy.GROUP1_FIELDS)) + ". Send an empty value ('' or []) "
        "to clear one. Lists replace the stored list, they do not append."))

    @model_validator(mode="after")
    def _bounded(self) -> "UpdateSettingsArgs":
        if len(json.dumps(self.changes, default=str)) > _MAX_CHANGES_BYTES:
            raise ValueError("changes is over 32 KB")
        return self


class UndoSettingsArgs(WriteArgs):
    """The `undo_id` a previous `lem_update_settings` answered with."""

    undo_id: str = Field(..., pattern=_UNDO_ID)


# --- reading and validating -------------------------------------------------------------------------


def _blob_name(resource: str) -> str:
    return policy.PREFS if resource == policy.ICP else resource


async def _read(lb: Loopback, resources: Any) -> dict[str, Any]:
    """The current stored record of each resource, keyed by blob name (ICP shares the preferences)."""
    blobs: dict[str, Any] = {}
    for resource in resources:
        name = _blob_name(resource)
        if name not in blobs:
            detail = await lb.get(_READ_PATHS[resource])
            blobs[name] = detail if isinstance(detail, dict) else {}
    return blobs


def _value(field: str, blobs: dict[str, Any]) -> Any:
    resource, key = policy.FIELD_TARGETS[field]
    return (blobs.get(_blob_name(resource)) or {}).get(key)


def _invalid(message: str) -> ToolFailure:
    return ToolFailure(INVALID_ARGS, "invalid_value", message)


def _validated(resource: str, sub: dict[str, Any], names: dict[str, str]) -> tuple[Any, list[str]]:
    """Run one resource's values through the route's own request model; return it plus its notes.

    Raises:
        ToolFailure: A value the route would refuse, naming the field and the reason, never the value.
    """
    from cqc_lem.api.routers import user as routes  # lazy: the router imports the app

    model_cls = {policy.PREFS: routes.EngagementPreferencesRequest,
                 policy.ICP: routes.IcpDefinitionRequest,
                 policy.LEAD_MAGNET: routes.LeadMagnetRequest,
                 policy.NEWSLETTER: routes.NewsletterSettingsRequest,
                 policy.TIMEZONE: routes.TimezoneRequest}[resource]
    try:
        model = model_cls.model_validate({"session_token": "-", **sub})
    except ValidationError as exc:
        problems = []
        for error in exc.errors(include_input=False, include_url=False, include_context=False):
            loc = str(error["loc"][0]) if error.get("loc") else "(value)"
            problems.append(f"{names.get(loc, loc)}: {error.get('msg')}")
        raise _invalid("LEM would reject these values. " + "; ".join(problems[:10])) from None
    notes: list[str] = []
    dropped = getattr(model, "dropped_forbidden_claim_terms", None)
    if dropped:
        notes.append("These forbidden_claim_terms were not kept (too short, too long, no letters or "
                     "past the 50-term cap): " + ", ".join(dropped))
    return model, notes


def _extra_checks(resource: str, sub: dict[str, Any], names: dict[str, str]) -> None:
    """The checks a route makes in its handler body rather than its model, run here to name the field."""
    if resource == policy.LEAD_MAGNET and sub.get("keyword"):
        from cqc_lem.utilities.linkedin_formatter import is_bait_keyword

        if is_bait_keyword(sub["keyword"]):
            raise _invalid("lead_magnet_keyword collides with the engagement-bait filter (words "
                           "like YES, AGREE, BELOW, AMEN, ME). Choose a distinctive word such as "
                           "AUDIT or GUIDE.")
    if resource == policy.NEWSLETTER and sub.get("cadence") not in (None, *policy.NEWSLETTER_CADENCES):
        raise _invalid("newsletter_cadence must be one of: " + ", ".join(policy.NEWSLETTER_CADENCES))
    if resource == policy.TIMEZONE:
        from zoneinfo import available_timezones

        if sub.get("timezone") not in available_timezones():
            raise _invalid("timezone must be an IANA zone name such as America/Chicago")


def _normalise(changes: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Every allowed change as the route would store it: `{field: value}`, plus notes to relay.

    Raises:
        ToolFailure: Any field the tool will not apply (`isError`, naming each), or a bad value.
    """
    refused = policy.refusals(changes)
    if refused:
        first = refused[0]
        raise ToolFailure(REFUSED, first.code,
                          " ".join(r.message for r in refused[:5]))
    nulls = [f for f, v in changes.items() if v is None and f not in policy.NULLABLE]
    if nulls:
        raise _invalid("null is not a value for: " + ", ".join(sorted(nulls))
                       + ". Send an empty value ('' or []) to clear a setting.")
    normal: dict[str, Any] = {}
    notes: list[str] = []
    for resource, fields in policy.by_resource(changes).items():
        sub = {policy.FIELD_TARGETS[f][1]: changes[f] for f in fields}
        names = {policy.FIELD_TARGETS[f][1]: f for f in fields}
        _extra_checks(resource, sub, names)
        model, extra_notes = _validated(resource, sub, names)
        notes += extra_notes
        for field in fields:
            normal[field] = getattr(model, policy.FIELD_TARGETS[field][1])
    return normal, notes


# --- the undo record ------------------------------------------------------------------------------


def _undo_key(user_id: int, undo_id: str) -> str:
    return f"mcp:settings-undo:{int(user_id)}:{undo_id}"


def _save_undo(user_id: int, record: dict[str, Any]) -> Optional[str]:
    """Store one undo record for seven days; None when Redis could not take it. Blocking."""
    client = writes._redis()
    if client is None:
        return None
    undo_id = secrets.token_hex(8)
    try:
        client.set(_undo_key(user_id, undo_id), json.dumps(record, default=str),
                   ex=UNDO_TTL_SECONDS)
    except Exception as exc:  # noqa: BLE001 — the change landed; a lost undo only loses the undo.
        log_warning("MCP settings undo record not stored", error_type=type(exc).__name__)
        return None
    return undo_id


def _load_undo(user_id: int, undo_id: str) -> Optional[dict[str, Any]]:
    """The undo record, or None when it expired, never existed or is another user's. Blocking."""
    client = writes._redis()
    if client is None:
        return None
    try:
        raw = client.get(_undo_key(user_id, undo_id))
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", "replace")
        record = json.loads(raw) if raw else None
    except Exception as exc:  # noqa: BLE001
        log_debug("MCP settings undo record not read", error_type=type(exc).__name__)
        return None
    if not isinstance(record, dict) or not isinstance(record.get("before"), dict) \
            or not isinstance(record.get("after"), dict):
        return None
    return record


def _drop_undo(user_id: int, undo_id: str) -> None:
    client = writes._redis()
    if client is None:
        return
    try:
        client.delete(_undo_key(user_id, undo_id))
    except Exception as exc:  # noqa: BLE001
        log_debug("MCP settings undo record not dropped", error_type=type(exc).__name__)


# --- writing --------------------------------------------------------------------------------------


async def _apply(lb: Loopback, values: dict[str, Any]) -> list[str]:
    """PUT `values` (`{field: value}`) one resource at a time, sending only those keys.

    Returns:
        The fields that landed. On a failure part-way the exception carries what already landed in
        `applied` (see `_Partial`).

    Raises:
        _Partial: A route refused or failed after an earlier resource had been written.
        ToolFailure: The first write failed: nothing changed.
    """
    done: list[str] = []
    for resource, fields in policy.by_resource(values).items():
        body = {policy.FIELD_TARGETS[f][1]: values[f] for f in fields}
        none_keys = frozenset(k for k, v in body.items() if v is None)
        lb.attempted_fields.extend(fields)
        try:
            await lb.write(PUT, _WRITE_PATHS[resource], body, keep_none=none_keys)
            lb.landed_fields.extend(fields)
        except ToolFailure as failure:
            if done:
                raise _Partial(failure, done) from None
            raise
        done += fields
    return done


class _Partial(Exception):
    """A multi-resource write that failed after some fields had already landed."""

    def __init__(self, failure: ToolFailure, applied: list[str]) -> None:
        super().__init__(failure.message)
        self.failure = failure
        self.applied = applied


def _partial_failure(partial: _Partial, undo_id: Optional[str]) -> ToolFailure:
    tail = (f" Already applied before it failed: {', '.join(partial.applied)}."
            + (f" Undo them with undo_id {undo_id}." if undo_id else ""))
    failure = partial.failure
    return ToolFailure(failure.outcome, failure.code, failure.message + tail, failure.inner_status)


async def _update_settings(lb: Loopback, args: UpdateSettingsArgs) -> dict[str, Any]:
    user_id = lb.principal.user_id
    normal, notes = _normalise(args.changes)
    blobs = await _read(lb, {policy.FIELD_TARGETS[f][0] for f in normal})
    before = {f: _value(f, blobs) for f in normal}
    to_write = {f: v for f, v in normal.items() if not policy.same(before[f], v)}
    unchanged = [f for f in normal if f not in to_write]
    if not to_write:
        return {"changed": {}, "unchanged": unchanged, "undo_id": None, "notes": notes,
                "note": "Nothing to change: every value already matches.",
                "review_url": _spa_url("/account")}
    applied: list[str] = []
    partial: Optional[_Partial] = None
    try:
        applied = await _apply(lb, to_write)
    except _Partial as exc:
        partial, applied = exc, exc.applied
    after = {f: normal[f] for f in applied}
    try:
        # What the route STORED, not what was asked: a clamp or a tidy is the `after` value.
        fresh = await _read(lb, {policy.FIELD_TARGETS[f][0] for f in applied})
        after = {f: _value(f, fresh) for f in applied}
    except ToolFailure as failure:
        log_debug("MCP settings read-back failed; reporting the requested values",
                  user_id=user_id, outcome=failure.outcome)
    changed = {f: {"before": before[f], "after": after[f]} for f in applied
               if not policy.same(before[f], after[f])}
    unchanged += [f for f in applied if f not in changed]
    undo_id = None
    if changed:
        undo_id = await run_in_threadpool(_save_undo, user_id, {
            "before": {f: before[f] for f in changed}, "after": {f: after[f] for f in changed}})
    if partial is not None:
        raise _partial_failure(partial, undo_id)
    answer: dict[str, Any] = {"changed": changed, "unchanged": unchanged, "undo_id": undo_id,
                              "notes": notes, "note": APPLIED_NOTE,
                              "review_url": _spa_url("/account")}
    if changed and undo_id is None:
        answer["undo_note"] = "Undo is unavailable for this change (its record could not be saved)."
    return answer


async def _undo_settings(lb: Loopback, args: UndoSettingsArgs) -> dict[str, Any]:
    user_id = lb.principal.user_id
    record = await run_in_threadpool(_load_undo, user_id, args.undo_id)
    if record is None:
        raise ToolFailure(REFUSED, "undo_not_found",
                          "That undo_id is unknown, expired (7 days) or already used.")
    after, before = record["after"], record["before"]
    fields = [f for f in after if f in policy.FIELD_TARGETS]
    blobs = await _read(lb, {policy.FIELD_TARGETS[f][0] for f in fields})
    conflicts = [f for f in fields if not policy.same(_value(f, blobs), after[f])]
    if conflicts:
        raise ToolFailure(REFUSED, "undo_conflict",
                          "These settings were changed again after that change, so nothing was "
                          "restored: " + ", ".join(sorted(conflicts)) + ". Ask the user what they "
                          "want, or change them with lem_update_settings.")
    values: dict[str, Any] = {}
    skipped: list[str] = []
    for field in fields:
        value = policy.restore_value(field, before.get(field))
        if value is policy.SKIP:
            skipped.append(field)
        else:
            values[field] = value
    try:
        restored = await _apply(lb, values) if values else []
    except _Partial as exc:
        raise _partial_failure(exc, None) from None
    if len(restored) == len(fields):
        await run_in_threadpool(_drop_undo, user_id, args.undo_id)
    return {"restored": {f: {"from": after[f], "to": before.get(f)} for f in restored},
            "skipped": skipped,
            "note": "Restored to the values before that change.",
            "review_url": _spa_url("/account")}


# --- the registry ---------------------------------------------------------------------------------

SETTINGS_TOOLS: tuple[ToolSpec, ...] = (
    ToolSpec("lem_update_settings", "Change account settings",
             "Change some of the user's LEM settings and leave the rest as they are: voice "
             "(tone, comment length and style, emojis, hashtags, forbidden claim terms, brand "
             "kit), targeting (topics, keywords, authors, focus topics, minimum reactions, post "
             "age), strategy (goals, buyer stage, audience mix, ICP), cadence (posts per week, "
             "posting days, session windows, timezone) and assets (lead magnet, newsletter topic "
             "and cadence). It applies at once, with no approval, so confirm the values with the "
             "user first and tell them what changed. Returns before and after for each field and "
             "an undo_id. It cannot change daily caps, approval modes or any safety gate, "
             "credentials, identity or billing: use lem_open_in_lem for those.",
             UpdateSettingsArgs, BOTH_SCOPES, _SETTINGS_CALLS, _update_settings, write=True,
             flag=MCP_SETTINGS_WRITES, audit_event=AuthAuditEvent.MCP_SETTINGS),
    ToolSpec("lem_undo_settings_change", "Undo a settings change",
             "Put back the values a lem_update_settings call changed, using its undo_id (kept 7 "
             "days). Refuses, naming the fields, if any of them was changed again since.",
             UndoSettingsArgs, BOTH_SCOPES, _SETTINGS_CALLS, _undo_settings, write=True,
             flag=MCP_SETTINGS_WRITES, audit_event=AuthAuditEvent.MCP_SETTINGS),
)

__all__ = ["SETTINGS_TOOLS", "UpdateSettingsArgs", "UndoSettingsArgs", "UNDO_TTL_SECONDS"]
