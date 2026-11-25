"""Which settings an MCP agent may change, and what happens to the rest (epic #2797, #2800).

ONE module, frozen in code: the allowlist the `lem_update_settings` tool reads AND the allowlist the
write routes re-check server-side for an MCP seat, so a tool bug cannot widen what a token may change.
Pure data and pure functions: no I/O, no `cqc_lem.*` imports, so a route can import it without
pulling in the tool registry.

A field is in exactly one **group**, and the group decides what the tool does with it:

- **Group 1** (`GROUP1_*`, owner ruling 1): voice, targeting, strategy, cadence and assets. Applied
  directly, through the same routes and validators the SPA uses.
- **Caps** (group 2, owner ruling 2): refused today, pointing at LEM. Issue #2801 turns `CAPS` into a
  handled group (warning on a raise, `acknowledge_risk`) by moving it into `HANDLED` below.
- **Group 3** (owner ruling 3): anything that loosens a safety or approval gate. Refused, naming the
  change-request path. Issue #2803 routes these to a request the USER approves in LEM.
- **Never** (owner ruling 5): credentials, identity, billing. Refused for good.
- Anything else is unknown and refused: a new setting is opt-IN, never opt-out.

Group 1 is split by the resource that stores it (`RESOURCES`), because each lives behind its own
route with its own write shape: the engagement-preferences row is replaced WHOLE by its PUT, so the
tool must merge (read, overlay, write the full record), never send a partial.
"""

import re
from dataclasses import dataclass
from typing import Any, Mapping

# --- the group-1 allowlist, per resource --------------------------------------------------------------

# `engagement_preferences` columns, written by `PUT /user/engagement-preferences`. Names are the
# route model's own (`EngagementPreferencesRequest`).
PREFS_FIELDS = frozenset({
    # voice
    "tone", "comment_length", "comment_style", "use_emojis", "use_hashtags",
    "forbidden_claim_terms", "brand_kit",
    # targeting
    "include_topics", "exclude_topics", "include_keywords", "exclude_keywords",
    "include_authors", "exclude_authors", "focus_topics", "min_reactions", "max_post_age_hours",
    # strategy
    "business_goals", "personal_goals", "default_buyer_stage", "audience_mix",
    # cadence
    "posts_per_week", "posting_days", "session_windows", "session_timezone",
})

# The ICP lives in the same row but is WRITTEN through its own route, which validates the definition
# against the rubric model (`PUT /user/icp-definition`). The prefs route has no such validator, so
# an MCP seat may not change these through it (`PREFS_EDITABLE` excludes them).
ICP_FIELDS = frozenset({"icp_definition", "icp_builder_enabled"})

# Tool field name -> the route's own key.
LEAD_MAGNET_FIELDS: Mapping[str, str] = {
    "lead_magnet_enabled": "enabled", "lead_magnet_keyword": "keyword",
    "lead_magnet_message": "message",
}
# Topic and cadence only. Auto-publish, `enabled`, covers and invites are not group 1.
NEWSLETTER_FIELDS: Mapping[str, str] = {
    "newsletter_topic": "topic", "newsletter_cadence": "cadence",
}
TIMEZONE_FIELDS: Mapping[str, str] = {"timezone": "timezone"}
# The cadences the newsletter card offers; the route stores any string, so the tool holds the line.
NEWSLETTER_CADENCES = ("weekly", "biweekly", "monthly")

# What the server lets an MCP seat SEND on each PUT, by the route's own key. A body naming anything
# else is a 403 (`routers/user._mcp_settings_guard`), whatever tool sent it.
PREFS_EDITABLE = PREFS_FIELDS
NEWSLETTER_EDITABLE = frozenset(NEWSLETTER_FIELDS.values())
LEAD_MAGNET_EDITABLE = frozenset(LEAD_MAGNET_FIELDS.values())
ICP_EDITABLE = ICP_FIELDS
TIMEZONE_EDITABLE = frozenset(TIMEZONE_FIELDS.values())

# Resource ids, in the order a multi-resource change is applied.
PREFS, ICP, LEAD_MAGNET, NEWSLETTER, TIMEZONE = (
    "preferences", "icp", "lead_magnet", "newsletter", "timezone")
RESOURCES = (PREFS, ICP, LEAD_MAGNET, NEWSLETTER, TIMEZONE)

# Tool field -> (resource, the route's own key).
FIELD_TARGETS: Mapping[str, tuple[str, str]] = {
    **{name: (PREFS, name) for name in PREFS_FIELDS},
    **{name: (ICP, name) for name in ICP_FIELDS},
    **{name: (LEAD_MAGNET, key) for name, key in LEAD_MAGNET_FIELDS.items()},
    **{name: (NEWSLETTER, key) for name, key in NEWSLETTER_FIELDS.items()},
    **{name: (TIMEZONE, key) for name, key in TIMEZONE_FIELDS.items()},
}
GROUP1_FIELDS = frozenset(FIELD_TARGETS)

# --- everything that is NOT group 1 -------------------------------------------------------------------

GROUP3_FIELDS = frozenset({
    "profile_viewer_dm_auto_send", "roster_auto_connect", "roster_auto_follow",
    "hold_repaired_posts_for_review", "authenticity_score_min", "post_similarity_max_pct",
    "auto_publish_newsletters", "newsletter_auto_publish", "auto_publish", "auto_schedule_posts",
})
# Every `*_mode` is an approval or targeting mode: `auto_approve` is a gate loosened.
_GROUP3_PATTERN = re.compile(r"^[a-z0-9_]+_mode$")

CAP_FIELDS = frozenset({
    "max_comments_per_day", "max_dms_per_day", "max_invites_per_day", "max_follows_per_day",
    "max_company_page_invites_per_day", "max_catchup_touches_per_day",
    "max_catchup_touches_per_contact_days", "min_catchup_contact_interval_days",
    "reply_sweeps_per_day", "reply_max_post_age_days", "max_queued_drafts",
    "max_invites_per_run",
})
_CAP_PATTERN = re.compile(r"^max_[a-z0-9_]+_per_(day|week|run|contact_days)$")

NEVER_FIELDS = frozenset({
    "linkedin_password", "linkedin_cookie", "li_at", "password", "cookie", "location", "latitude",
    "longitude", "city", "country", "display_name", "linkedin_display_name", "name",
    "company_page", "company_linkedin_url", "email", "token", "session_token", "api_key",
    "session", "sessions", "billing", "subscription", "stripe_customer_id", "plan", "tier",
    "reply_inbound_address", "reply_inbound_token",
})

KIND_ALLOWED, KIND_GROUP3, KIND_CAP, KIND_NEVER, KIND_UNKNOWN = (
    "allowed", "group3", "cap", "never", "unknown")

# Kinds the tool applies. #2801 adds KIND_CAP here (with its own warning handling), and #2803 routes
# KIND_GROUP3 to a change request: until then each is refused with its own code and message.
HANDLED = frozenset({KIND_ALLOWED})


@dataclass(frozen=True)
class Refusal:
    """Why one field is not changed by the tool: a stable code and a message safe to show."""

    field: str
    kind: str
    code: str
    message: str


def classify(field: str) -> str:
    """The kind of one field name: allowed, group3, cap, never or unknown.

    Args:
        field: The field name the agent sent, exactly as sent.

    Returns:
        One of the `KIND_*` constants. Order matters: group 1 first (an exact, frozen set), then the
        refusals from the most to the least specific, so a name that is both a cap and unknown reads
        as the cap.
    """
    if field in GROUP1_FIELDS:
        return KIND_ALLOWED
    if field in GROUP3_FIELDS or _GROUP3_PATTERN.match(field):
        return KIND_GROUP3
    if field in CAP_FIELDS or _CAP_PATTERN.match(field):
        return KIND_CAP
    if field in NEVER_FIELDS:
        return KIND_NEVER
    return KIND_UNKNOWN


_REFUSALS: Mapping[str, tuple[str, str]] = {
    KIND_GROUP3: ("needs_change_request",
                  "'{field}' loosens a safety or approval gate, so an agent cannot change it. It "
                  "needs a change request (coming) that the user approves in LEM. For now, tell "
                  "the user and open it with lem_open_in_lem (destination: account)."),
    KIND_CAP: ("cap_not_editable",
               "'{field}' is a daily cap and is not editable through this tool. Tell the user and "
               "open it with lem_open_in_lem (destination: account)."),
    KIND_NEVER: ("field_never_editable",
                 "'{field}' is never changed through MCP (credentials, identity and billing stay "
                 "with the user). Open it with lem_open_in_lem (destination: account)."),
    KIND_UNKNOWN: ("field_not_allowed",
                   "'{field}' is not a setting this tool can change. Allowed fields: {allowed}. "
                   "For anything else use lem_open_in_lem (destination: account)."),
}


def refusals(fields: Any) -> list[Refusal]:
    """Every field in `fields` the tool will not apply, in the order given.

    Args:
        fields: An iterable of the field names the agent sent.

    Returns:
        One `Refusal` per field whose kind is not in `HANDLED`; empty when all may be applied.
    """
    out: list[Refusal] = []
    allowed = ", ".join(sorted(GROUP1_FIELDS))
    for field in fields:
        kind = classify(str(field))
        if kind in HANDLED:
            continue
        code, template = _REFUSALS[kind]
        out.append(Refusal(str(field), kind, code, template.format(field=str(field)[:60],
                                                                    allowed=allowed)))
    return out


def by_resource(fields: Any) -> dict[str, list[str]]:
    """Group allowed field names by the resource that stores them, in `RESOURCES` order.

    Args:
        fields: Allowed (group-1) field names.

    Returns:
        `{resource: [field, ...]}` for the resources that have at least one field.
    """
    grouped: dict[str, list[str]] = {}
    for resource in RESOURCES:
        names = [f for f in fields if FIELD_TARGETS[f][0] == resource]
        if names:
            grouped[resource] = names
    return grouped


def canon(value: Any) -> Any:
    """A comparable form of a stored or sent value: bools as ints, dict order and tuples erased.

    An unset value (None, "", [], {}) is ONE value: the routes spell "cleared" differently from the
    way MySQL hands it back, and a round trip must not read as a change.

    MySQL hands booleans back as 0/1 while the route model says `True`; two values that mean the
    same thing must compare equal or a round trip looks like a change.
    """
    if value is None or (isinstance(value, (str, list, tuple, Mapping)) and len(value) == 0):
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, Mapping):
        return {str(k): canon(v) for k, v in sorted(value.items(), key=lambda kv: str(kv[0]))}
    if isinstance(value, (list, tuple)):
        return [canon(v) for v in value]
    return value


def same(a: Any, b: Any) -> bool:
    """Whether two settings values mean the same thing (`canon` equality)."""
    return canon(a) == canon(b)


def audit_fields(payload: Mapping[str, Any]) -> list[str]:
    """The field NAMES a tool answer changed or restored: all an audit row ever carries."""
    for key in ("changed", "restored"):
        value = payload.get(key)
        if isinstance(value, Mapping):
            return sorted(str(k) for k in value)
    return []


# Fields whose stored NULL is a real value the routes accept back as an explicit `null`: restoring
# one writes NULL (`Loopback.write(keep_none=...)`).
NULLABLE = frozenset({
    "tone", "comment_style", "business_goals", "personal_goals", "default_buyer_stage",
    "min_reactions", "max_post_age_hours", "lead_magnet_keyword", "lead_magnet_message",
    "newsletter_topic",
})
# Fields where the prefs route reads `null` as "leave what is stored", so "unset" is spelled with
# the empty value the route documents.
_CLEAR_VALUES: Mapping[str, Any] = {
    "forbidden_claim_terms": [], "brand_kit": {}, "audience_mix": {}, "session_windows": [],
    "session_timezone": "", "icp_definition": {},
}
SKIP = object()


def restore_value(field: str, before: Any) -> Any:
    """What to send to put `field` back to a stored `before`; `SKIP` when it cannot be expressed.

    Args:
        field: The tool field name.
        before: The value read before the change (None when it was NULL).

    Returns:
        `before` when set; otherwise None for a NULLABLE field, the route's "clear" value where
        `null` would mean "keep", and `SKIP` for a field that has no way to be put back to NULL.
    """
    if before is not None:
        return before
    if field in NULLABLE:
        return None
    return _CLEAR_VALUES.get(field, SKIP)
