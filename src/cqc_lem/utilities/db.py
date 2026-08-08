"""Every SQL statement LEM runs. No raw SQL lives outside this module.

Readers here fail SOFT: a `mysql.connector.Error` is logged and turned into the empty answer for that
shape (None / [] / 0 / False), so a database blip degrades one feature instead of raising through a
Celery task or an API handler. Where that default would be the dangerous answer the function fails
the other way and says so — `has_received_lead_magnet` returns True on error so a fault never
double-DMs, and `user_owns_posts` raises `OwnershipUnprovable` rather than answer an authorisation
question it could not run (issue #914).

Secrets are sealed and unsealed here and nowhere else (issue #745): `li_at` and the other cookie
values, the OAuth tokens and the stored password, keyed per user+column off `LEM_SECRET_KEY` with the
`SECRET_FIELD_*` constants as AAD. Renaming one of those constants orphans every row already written
under it, and a value that will not decrypt reads as None rather than as ciphertext.
"""

import hashlib
import json
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional, Union

import mysql.connector
from dotenv import load_dotenv
from mysql.connector import errorcode
from mysql.connector.abstracts import MySQLCursorAbstract

from cqc_lem.platform.db import connection as _connection
from cqc_lem.platform.db.connection import (
    MYSQL_DATABASE,
    MYSQL_HOST,
    MYSQL_PASSWORD,
    MYSQL_PORT,
    MYSQL_USER,
    DbConnection,
    _get_connection_pool,
    _get_mysql_config,
    _get_pooled_connection,
    _PoolState,
    db_cursor,
    get_db_connection,
    reset_connection_pool,
    to_naive_utc,
)
from cqc_lem.platform.db.enums import (
    AffiliateRewardKind,
    AffiliateStatus,
    AuthAuditEvent,
    CatchupEventType,
    CatchupTouchStatus,
    ConnectionRequestStatus,
    ConnectStatus,
    CostCategory,
    FaqStatus,
    FeedbackSource,
    FeedbackStatus,
    FollowStatus,
    GroupPostDraftStatus,
    LeadSignalChannel,
    LeadSignalKind,
    LeadSignalSource,
    LeadSignalStatus,
    LeadStage,
    LogActionType,
    LogResultType,
    OnboardingStep,
    OutreachStage,
    OutreachStatus,
    PostStatus,
    PostType,
    ReferralStatus,
    ScheduledDmStatus,
)
from cqc_lem.platform.db.repositories.auth import (
    AUTH_FACTOR_TOTP,
    SESSION_SCOPE_AGENT,
    SESSION_SCOPE_ENROLL,
    TOTP_SECRET_FIELD,
    claim_auth_challenge_attempt,
    clear_challenge_attempts,
    confirm_totp_factor,
    consume_auth_challenge,
    consume_recovery_code,
    count_auth_factors,
    count_challenge_attempts,
    count_recovery_codes,
    create_auth_challenge,
    create_pin_for_email,
    delete_auth_factor,
    delete_pin_for_email,
    delete_session,
    finish_auth_challenge,
    get_app_credential,
    get_app_credential_updated_at,
    get_auth_audit_events,
    get_pin_lockout,
    get_session_auth_state,
    get_session_id,
    get_totp_factor,
    get_unused_recovery_codes,
    get_user_passkey_credential_ids,
    list_auth_factors,
    mark_session_verified,
    record_auth_event,
    release_enrollment_scope,
    replace_recovery_codes,
    resolve_session,
    revoke_other_sessions,
    revoke_session,
    set_app_credential,
    set_session_scope,
    touch_auth_factor,
    update_factor_counter,
    upsert_totp_factor,
    verify_pin_for_email,
)
from cqc_lem.platform.db.repositories.avatar import (
    AVATAR_APPROVAL_APPROVED,
    AVATAR_APPROVAL_REJECTED,
    add_avatar_credits,
    add_video_credits,
    claim_avatar_sample_render,
    deduct_avatar_credit,
    deduct_video_credits,
    get_avatar_credit_balance,
    get_avatar_credit_ledger_entry_by_session,
    get_video_credit_balance,
    get_video_credit_ledger_entry_by_session,
    insert_avatar_training,
    refund_avatar_credit,
    refund_video_credits,
    release_avatar_sample_render,
    set_active_avatar,
    set_avatar_approval,
    update_avatar_attributes,
    update_avatar_samples,
    update_avatar_training_status,
)
from cqc_lem.platform.db.repositories.billing import (
    COST_ROLLUP_COLUMNS,
    accrue_monthly_fixed_costs,
    convert_affiliate_referral,
    get_affiliate_referral_counts,
    get_affiliate_referral_for_referred,
    get_affiliate_reward_totals,
    get_cost_rollup,
    get_daily_cost_totals,
    get_early_adopter_grant,
    get_early_adopter_slot_usage,
    insert_cost_ledger_entry,
    mark_affiliate_notice_seen,
    record_affiliate_referral,
)
from cqc_lem.platform.db.repositories.engagement import (
    CLAIM_STALE_MINUTES,
    COMPANY_PAGE_INVITE_SENT_MESSAGE,
    CONNECTION_REQUEST_SENT_MESSAGE,
    STALE_INVITE_WITHDRAWN_MESSAGE,
    _count_actions_today,
    claim_post_for_comment,
    count_comments_today,
    count_company_page_invites_sent_today,
    count_followup_replies_today,
    count_invite_withdrawals_today,
    count_invites_sent_today,
    count_user_comments_on_post_url,
    get_comment_followup,
    get_comment_outcome_targets,
    get_comment_outcomes,
    get_daily_action_counts,
    get_dm_history_for_profile,
    get_duplicate_comment_posts,
    get_post_age_minutes,
    get_post_message_from_log_for_user,
    get_post_url_from_log_for_user,
    get_recent_comment_texts,
    get_recent_commented_rows_with_text,
    get_recent_logs,
    get_recent_navigable_commented_posts,
    has_automated_engagement,
    has_commented_post,
    has_engaged_url_with_x_days,
    insert_new_log,
    mark_post_commented,
    mark_post_reacted,
    record_comment_followup,
    record_comment_outcome,
    release_post_claim,
    update_commented_post_key,
)
from cqc_lem.platform.db.repositories.feedback import (
    _FAQ_COLUMNS,
    _LEN_STORY_TITLE,
    STORY_BANK_KINDS,
    apply_faq_entry_version,
    count_feedback_filed_by_user,
    count_story_bank_entries,
    delete_story_bank_entry,
    get_faq_candidate_feedback,
    get_faq_entries,
    get_faq_entry_by_cluster,
    get_faq_entry_versions,
    get_feedback_by_id,
    get_feedback_reporters_for_issue,
    get_latest_feedback_at,
    get_latest_review_feedback_id,
    get_open_feedback_clusters,
    get_published_faq_entries,
    get_survey_prompts_sent,
    has_review_feedback,
    insert_feedback,
    mark_feedback_resolved_for_issue,
    record_faq_entry_version,
    record_feedback_review,
    record_story_bank_use,
    record_survey_prompt,
    update_feedback_triage,
    upsert_faq_entry,
    upsert_story_bank_entries,
)
from cqc_lem.platform.db.repositories.groups import (
    create_group_post_draft,
    get_enabled_group_ids,
    get_next_group_for_post,
    get_post_enabled_group_ids,
    get_user_groups,
    record_group_post,
    record_group_post_run,
    set_groups_enabled,
    update_group_post_draft,
    upsert_user_group,
)
from cqc_lem.platform.db.repositories.newsletter import (
    _NEWSLETTER_BOOL_COLS,
    _NEWSLETTER_COLS,
    _NEWSLETTER_DEFAULTS,
    clear_edition_cover_image,
    count_pending_newsletter_editions,
    create_newsletter_edition,
    get_editions_due_to_publish,
    get_enabled_newsletter_user_ids,
    get_latest_edition_scheduled_for,
    get_latest_newsletter_subscriber_count,
    get_newsletter_due_user_ids,
    get_newsletter_edition,
    get_newsletter_settings,
    get_newsletter_subscriber_stats,
    get_pending_newsletter_edition,
    get_pending_newsletter_editions,
    get_recent_newsletter_blueprint_history,
    get_recent_newsletter_subjects,
    get_recent_shipped_notices,
    get_shipped_notice_by_issue,
    get_shipped_notice_recipient_ids,
    get_unseen_shipped_notices,
    mark_edition_failed,
    mark_edition_published,
    mark_newsletter_published,
    mark_shipped_notice_seen,
    record_newsletter_subscriber_stat,
    record_shipped_notice,
    record_shipped_notice_recipient,
    set_edition_cover_image,
    set_edition_cover_status,
    update_newsletter_edition,
    update_newsletter_settings,
)
from cqc_lem.platform.db.repositories.outreach import (
    _CATCHUP_COLS,
    _CONN_REQ_COLS,
    _DM_DEFAULT_TEMPLATES,
    _LEAD_COLS,
    _LEAD_MAGNET_DEFAULTS,
    _LEAD_SIGNAL_COLS,
    _OPEN_SCHED_DM_STATUSES,
    _OUTREACH_COLS,
    _SCHED_DM_COLS,
    ENGAGEMENT_TARGET_CATEGORIES,
    ENGAGEMENT_TARGET_FOLLOW_MAX_ATTEMPTS,
    ENGAGEMENT_TARGET_SOURCES,
    ENGAGEMENT_TARGET_WEEKLY_MAX,
    claim_appreciation_touch,
    claim_catchup_send_attempt,
    count_catchup_touches_for_contact_in_window,
    count_catchup_touches_sent_today,
    count_hot_leads,
    count_new_lead_signals,
    count_open_connection_requests,
    count_open_outreach_targets,
    count_pending_catchup_touches,
    count_scheduled_dms_created_today,
    delete_engagement_target,
    enqueue_followup,
    get_approved_catchup_touches,
    get_approved_connection_requests,
    get_approved_outreach_targets,
    get_catchup_touch,
    get_catchup_touches,
    get_connection_request,
    get_connection_requests,
    get_dm_template,
    get_dm_templates,
    get_due_followups,
    get_due_scheduled_dms,
    get_hot_leads,
    get_lead,
    get_lead_magnet_settings,
    get_lead_signal,
    get_lead_signals,
    get_leads,
    get_orphaned_catchup_touches,
    get_orphaned_connection_requests,
    get_orphaned_scheduled_dms,
    get_outreach_target,
    get_outreach_target_by_url,
    get_outreach_targets,
    get_requested_person_keys,
    get_scheduled_dm,
    get_scheduled_dms,
    get_users_with_approved_outreach,
    has_appreciation_touch,
    has_catchup_touch,
    has_lead_signal,
    has_open_scheduled_dm,
    has_received_lead_magnet,
    insert_catchup_touch,
    insert_connection_request,
    insert_lead_signal,
    insert_outreach_target,
    insert_scheduled_dm,
    last_catchup_sent_at,
    mark_followup,
    record_lead_magnet_sent,
    record_target_comment_blocked,
    record_target_follow_failure,
    release_catchup_send_attempt,
    reset_lead_scores,
    set_target_connect_status,
    set_target_follow_status,
    stop_followups_for_profile,
    update_catchup_touch,
    update_catchup_touch_status,
    update_connection_request,
    update_connection_request_status,
    update_lead,
    update_lead_magnet_settings,
    update_lead_signal,
    update_outreach_target,
    update_outreach_target_status,
    update_scheduled_dm,
    update_scheduled_dm_status,
    upsert_dm_templates,
    upsert_engagement_targets,
    upsert_lead,
)
from cqc_lem.platform.db.shared import (
    _FEEDBACK_COLUMNS,
    AUTH_FACTOR_PASSKEY,
    AVATAR_APPROVAL_PENDING,
    DEFAULT_CONTENT_BUFFER_DAYS,
    DEFAULT_CONTENT_BUFFER_MAX_POSTS,
    ENGAGEMENT_TARGET_CONNECT_STATUSES,
    ENGAGEMENT_TARGET_FOLLOW_STATUSES,
    ENGAGEMENT_TARGET_WEEKLY_DEFAULT,
    MAX_CONTENT_BUFFER_DAYS,
    ONBOARDING_STEPS,
    SESSION_SCOPE_FULL,
    VALID_VIDEO_QUALITIES,
    BlockedVisit,
    OwnershipUnprovable,
)
from cqc_lem.utilities.crypto import (
    decrypt_secret,
    encrypt_secret,
    encryption_enabled,
    hash_client_ip,
    hash_session_token,
    needs_reencrypt,
)
from cqc_lem.utilities.env_constants import (
    SESSION_IDLE_HOURS,
)
from cqc_lem.utilities.linkedin.profile import LinkedInProfile
from cqc_lem.utilities.logger import log_debug, log_error, log_info, log_warning, myprint
from cqc_lem.utilities.utils import get_top_level_domain

# Re-exported for the ~2,400 call sites that still say `from cqc_lem.utilities.db import X`.
# `__all__` is the one declaration ruff (F401) and CodeQL (py/unused-import) both understand —
# a per-line ruff directive is invisible to CodeQL and an lgtm marker is invisible to ruff, so
# either one alone leaves whichever tool is blind free to flag or delete these.
__all__ = [
    "ENGAGEMENT_TARGET_CATEGORIES",
    "ENGAGEMENT_TARGET_FOLLOW_MAX_ATTEMPTS",
    "ENGAGEMENT_TARGET_SOURCES",
    "ENGAGEMENT_TARGET_WEEKLY_MAX",
    "_CATCHUP_COLS",
    "_CONN_REQ_COLS",
    "_DM_DEFAULT_TEMPLATES",
    "_LEAD_COLS",
    "_LEAD_MAGNET_DEFAULTS",
    "_LEAD_SIGNAL_COLS",
    "_OPEN_SCHED_DM_STATUSES",
    "_OUTREACH_COLS",
    "_SCHED_DM_COLS",
    "claim_appreciation_touch",
    "claim_catchup_send_attempt",
    "count_catchup_touches_for_contact_in_window",
    "count_catchup_touches_sent_today",
    "count_hot_leads",
    "count_new_lead_signals",
    "count_open_connection_requests",
    "count_open_outreach_targets",
    "count_pending_catchup_touches",
    "count_scheduled_dms_created_today",
    "delete_engagement_target",
    "enqueue_followup",
    "get_approved_catchup_touches",
    "get_approved_connection_requests",
    "get_approved_outreach_targets",
    "get_catchup_touch",
    "get_catchup_touches",
    "get_connection_request",
    "get_connection_requests",
    "get_dm_template",
    "get_dm_templates",
    "get_due_followups",
    "get_due_scheduled_dms",
    "get_hot_leads",
    "get_lead",
    "get_lead_magnet_settings",
    "get_lead_signal",
    "get_lead_signals",
    "get_leads",
    "get_orphaned_catchup_touches",
    "get_orphaned_connection_requests",
    "get_orphaned_scheduled_dms",
    "get_outreach_target",
    "get_outreach_target_by_url",
    "get_outreach_targets",
    "get_requested_person_keys",
    "get_scheduled_dm",
    "get_scheduled_dms",
    "get_users_with_approved_outreach",
    "has_appreciation_touch",
    "has_catchup_touch",
    "has_lead_signal",
    "has_open_scheduled_dm",
    "has_received_lead_magnet",
    "insert_catchup_touch",
    "insert_connection_request",
    "insert_lead_signal",
    "insert_outreach_target",
    "insert_scheduled_dm",
    "last_catchup_sent_at",
    "mark_followup",
    "record_lead_magnet_sent",
    "record_target_comment_blocked",
    "record_target_follow_failure",
    "release_catchup_send_attempt",
    "reset_lead_scores",
    "set_target_connect_status",
    "set_target_follow_status",
    "stop_followups_for_profile",
    "update_catchup_touch",
    "update_catchup_touch_status",
    "update_connection_request",
    "update_connection_request_status",
    "update_lead",
    "update_lead_magnet_settings",
    "update_lead_signal",
    "update_outreach_target",
    "update_outreach_target_status",
    "update_scheduled_dm",
    "update_scheduled_dm_status",
    "upsert_dm_templates",
    "upsert_engagement_targets",
    "upsert_lead",
    "AVATAR_APPROVAL_APPROVED",
    "AVATAR_APPROVAL_REJECTED",
    "add_avatar_credits",
    "add_video_credits",
    "claim_avatar_sample_render",
    "deduct_avatar_credit",
    "deduct_video_credits",
    "get_avatar_credit_balance",
    "get_avatar_credit_ledger_entry_by_session",
    "get_video_credit_balance",
    "get_video_credit_ledger_entry_by_session",
    "insert_avatar_training",
    "refund_avatar_credit",
    "refund_video_credits",
    "release_avatar_sample_render",
    "set_active_avatar",
    "set_avatar_approval",
    "update_avatar_attributes",
    "update_avatar_samples",
    "update_avatar_training_status",
    "STORY_BANK_KINDS",
    "_FAQ_COLUMNS",
    "_LEN_STORY_TITLE",
    "apply_faq_entry_version",
    "count_feedback_filed_by_user",
    "count_story_bank_entries",
    "delete_story_bank_entry",
    "get_faq_candidate_feedback",
    "get_faq_entries",
    "get_faq_entry_by_cluster",
    "get_faq_entry_versions",
    "get_feedback_by_id",
    "get_feedback_reporters_for_issue",
    "get_latest_feedback_at",
    "get_latest_review_feedback_id",
    "get_open_feedback_clusters",
    "get_published_faq_entries",
    "get_survey_prompts_sent",
    "has_review_feedback",
    "insert_feedback",
    "mark_feedback_resolved_for_issue",
    "record_faq_entry_version",
    "record_feedback_review",
    "record_story_bank_use",
    "record_survey_prompt",
    "update_feedback_triage",
    "upsert_faq_entry",
    "upsert_story_bank_entries",
    "AUTH_FACTOR_TOTP",
    "SESSION_SCOPE_AGENT",
    "SESSION_SCOPE_ENROLL",
    "TOTP_SECRET_FIELD",
    "claim_auth_challenge_attempt",
    "clear_challenge_attempts",
    "confirm_totp_factor",
    "consume_auth_challenge",
    "consume_recovery_code",
    "count_auth_factors",
    "count_challenge_attempts",
    "count_recovery_codes",
    "create_auth_challenge",
    "create_pin_for_email",
    "delete_auth_factor",
    "delete_pin_for_email",
    "delete_session",
    "finish_auth_challenge",
    "get_app_credential",
    "get_app_credential_updated_at",
    "get_auth_audit_events",
    "get_pin_lockout",
    "get_session_auth_state",
    "get_session_id",
    "get_totp_factor",
    "get_unused_recovery_codes",
    "get_user_passkey_credential_ids",
    "list_auth_factors",
    "mark_session_verified",
    "record_auth_event",
    "release_enrollment_scope",
    "replace_recovery_codes",
    "resolve_session",
    "revoke_other_sessions",
    "revoke_session",
    "set_app_credential",
    "set_session_scope",
    "touch_auth_factor",
    "update_factor_counter",
    "upsert_totp_factor",
    "verify_pin_for_email",
    "AUTH_FACTOR_PASSKEY",
    "AVATAR_APPROVAL_PENDING",
    "BlockedVisit",
    "DEFAULT_CONTENT_BUFFER_DAYS",
    "DEFAULT_CONTENT_BUFFER_MAX_POSTS",
    "ENGAGEMENT_TARGET_CONNECT_STATUSES",
    "ENGAGEMENT_TARGET_FOLLOW_STATUSES",
    "ENGAGEMENT_TARGET_WEEKLY_DEFAULT",
    "MAX_CONTENT_BUFFER_DAYS",
    "ONBOARDING_STEPS",
    "OwnershipUnprovable",
    "SESSION_SCOPE_FULL",
    "VALID_VIDEO_QUALITIES",
    "_FEEDBACK_COLUMNS",
    "COST_ROLLUP_COLUMNS",
    "accrue_monthly_fixed_costs",
    "convert_affiliate_referral",
    "get_affiliate_referral_counts",
    "get_affiliate_referral_for_referred",
    "get_affiliate_reward_totals",
    "get_cost_rollup",
    "get_daily_cost_totals",
    "get_early_adopter_grant",
    "get_early_adopter_slot_usage",
    "insert_cost_ledger_entry",
    "mark_affiliate_notice_seen",
    "record_affiliate_referral",
    "create_group_post_draft",
    "get_enabled_group_ids",
    "get_next_group_for_post",
    "get_post_enabled_group_ids",
    "get_user_groups",
    "record_group_post",
    "record_group_post_run",
    "set_groups_enabled",
    "update_group_post_draft",
    "upsert_user_group",
    "CLAIM_STALE_MINUTES",
    "COMPANY_PAGE_INVITE_SENT_MESSAGE",
    "CONNECTION_REQUEST_SENT_MESSAGE",
    "STALE_INVITE_WITHDRAWN_MESSAGE",
    "_count_actions_today",
    "claim_post_for_comment",
    "count_comments_today",
    "count_company_page_invites_sent_today",
    "count_followup_replies_today",
    "count_invite_withdrawals_today",
    "count_invites_sent_today",
    "count_user_comments_on_post_url",
    "get_comment_followup",
    "get_comment_outcome_targets",
    "get_comment_outcomes",
    "get_daily_action_counts",
    "get_dm_history_for_profile",
    "get_duplicate_comment_posts",
    "get_post_age_minutes",
    "get_post_message_from_log_for_user",
    "get_post_url_from_log_for_user",
    "get_recent_comment_texts",
    "get_recent_commented_rows_with_text",
    "get_recent_logs",
    "get_recent_navigable_commented_posts",
    "has_automated_engagement",
    "has_commented_post",
    "has_engaged_url_with_x_days",
    "insert_new_log",
    "mark_post_commented",
    "mark_post_reacted",
    "record_comment_followup",
    "record_comment_outcome",
    "release_post_claim",
    "update_commented_post_key",
    "ALREADY_CONNECTED_MESSAGE",
    "APPRECIATION_EVENT_TYPES",
    "CATCHUP_CONTACT_CAP_WINDOW_DAYS",
    "CATCHUP_TOUCHES_MAX",
    "CONNECT_NOTE_MAX_CHARS",
    "ENGAGEMENT_TARGET_BLOCKED_BADGE_STREAK",
    "ENGAGEMENT_TARGET_CONNECT_TERMINAL",
    "ENGAGEMENT_TARGET_FOLLOW_TERMINAL",
    "INVITE_NOT_SENT_MESSAGE",
    "MAX_WAIT_RETRY",
    "MYSQL_DATABASE",
    "MYSQL_HOST",
    "MYSQL_PASSWORD",
    "MYSQL_PORT",
    "MYSQL_USER",
    "NO_CONNECT_BUTTON_MESSAGE",
    "SCHEDULED_DM_SOURCE_NURTURE",
    "SESSION_SCOPE_EXTENSION",
    "SESSION_SCOPE_RECOVERY",
    "STORY_BANK_TARGET_ENTRIES",
    "_NEWSLETTER_BOOL_COLS",
    "_NEWSLETTER_COLS",
    "_NEWSLETTER_DEFAULTS",
    "clear_edition_cover_image",
    "count_pending_newsletter_editions",
    "create_newsletter_edition",
    "get_editions_due_to_publish",
    "get_enabled_newsletter_user_ids",
    "get_latest_edition_scheduled_for",
    "get_latest_newsletter_subscriber_count",
    "get_newsletter_due_user_ids",
    "get_newsletter_edition",
    "get_newsletter_settings",
    "get_newsletter_subscriber_stats",
    "get_pending_newsletter_edition",
    "get_pending_newsletter_editions",
    "get_recent_newsletter_blueprint_history",
    "get_recent_newsletter_subjects",
    "get_recent_shipped_notices",
    "get_shipped_notice_by_issue",
    "get_shipped_notice_recipient_ids",
    "get_unseen_shipped_notices",
    "mark_edition_failed",
    "mark_edition_published",
    "mark_newsletter_published",
    "mark_shipped_notice_seen",
    "record_newsletter_subscriber_stat",
    "record_shipped_notice",
    "record_shipped_notice_recipient",
    "set_edition_cover_image",
    "set_edition_cover_status",
    "update_newsletter_edition",
    "update_newsletter_settings",
    "AffiliateRewardKind",
    "AffiliateStatus",
    "AuthAuditEvent",
    "CatchupEventType",
    "CatchupTouchStatus",
    "ConnectStatus",
    "ConnectionRequestStatus",
    "CostCategory",
    "DbConnection",
    "FaqStatus",
    "FeedbackSource",
    "FeedbackStatus",
    "FollowStatus",
    "GroupPostDraftStatus",
    "LeadSignalChannel",
    "LeadSignalKind",
    "LeadSignalSource",
    "LeadSignalStatus",
    "LeadStage",
    "LogActionType",
    "LogResultType",
    "OnboardingStep",
    "OutreachStage",
    "OutreachStatus",
    "PostStatus",
    "PostType",
    "ReferralStatus",
    "ScheduledDmStatus",
    "_PoolState",
    "_get_connection_pool",
    "_get_mysql_config",
    "_get_pooled_connection",
    "db_cursor",
    "get_db_connection",
    "reset_connection_pool",
    "to_naive_utc",
]

# Load .env file
load_dotenv()

MAX_WAIT_RETRY = 3

# The MYSQL_* settings are NOT redefined here. `platform/db/connection.py` owns them and is what
# `get_db_connection` reads; a second `os.getenv` here produced a second binding that agreed by
# coincidence and diverged the moment anything set one. That is not hypothetical -- the e2e
# workflow test set MYSQL_HOST on this module to point at a host-machine MySQL and the override
# silently did nothing, because the connect it then called reads connection.MYSQL_HOST.
# Re-exported below so `from cqc_lem.utilities.db import MYSQL_HOST` still resolves.

































































_ONBOARDING_COLS: tuple = tuple(f"{step.value}_at" for step in ONBOARDING_STEPS)


# Enum for log actions types


# ENum for log result options





# Why a proactive invite was abandoned before it was attempted (issue #623). Stored as the request's
# failure_reason so the Connections review UI explains a FAILED row instead of just colouring it red.
ALREADY_CONNECTED_MESSAGE = "Already connected (1st-degree) — no invite to send"

# The profile offered no Connect affordance at all — neither the direct button nor one inside the
# More-actions menu (issue #571). Usually an invite is already pending or LinkedIn only offers
# Follow/Message on that profile; either way there is nothing to send, so the invite stops here
# rather than falling through to the note/send steps that can only fail after it.
NO_CONNECT_BUTTON_MESSAGE = "No Connect option on this profile (invite may already be pending)"

# The Connect dialog opened but neither Send affordance could be clicked, so nothing went out
# (issue #573). Unlike a missing note this does NOT degrade gracefully — the invite is lost — which
# is why it stays an error and gets its own reason on the request row.
INVITE_NOT_SENT_MESSAGE = "Connect dialog opened but the invitation could not be sent"

# LinkedIn's hard cap on a connection-request note. Also the point past which a drafted note is
# refined down rather than typed and silently truncated by the textarea's own maxlength.
CONNECT_NOTE_MAX_CHARS = 300


# Issue #745 (PR 2a) — the four columns holding a LinkedIn credential at rest. The names are the
# encryption AAD (`<table>.<column>`), so they are part of the ciphertext contract: renaming one
# makes every existing row undecryptable. db.py is the ONLY place encrypt/decrypt is called, so the
# ten modules that consume these secrets cannot accidentally bypass it. See utilities/crypto.py.
SECRET_FIELD_COOKIE_VALUE = "cookies.value"
SECRET_FIELD_ACCESS_TOKEN = "users.access_token"
SECRET_FIELD_REFRESH_TOKEN = "users.refresh_token"
SECRET_FIELD_PASSWORD = "users.password"

# The only statements the backfill/rotation pass may run — fixed literals, never composed from a
# row, so there is no path by which data becomes SQL.
_SECRET_UPDATE_SQL = {
    SECRET_FIELD_PASSWORD: "UPDATE users SET password = %s WHERE id = %s",
    SECRET_FIELD_ACCESS_TOKEN: "UPDATE users SET access_token = %s WHERE id = %s",
    SECRET_FIELD_REFRESH_TOKEN: "UPDATE users SET refresh_token = %s WHERE id = %s",
    SECRET_FIELD_COOKIE_VALUE: "UPDATE cookies SET value = %s WHERE id = %s",
}


def store_cookies(user_email: str, cookies: list[dict]) -> bool:
    """Persist the browser's cookies for this user. Returns False when any row failed to store.

    The return value is load-bearing since #745: the cookie-migration path DELETES the user's
    stored LinkedIn password once the session is "saved", so a swallowed per-row write error must
    not read as success — that would take away the only login they had left.
    """
    connection = _connection.get_db_connection()
    cursor = connection.cursor()

    user_id = get_user_id(user_email)

    try:
        failed = _store_cookie_rows(cursor, cookies, user_id)
        connection.commit()
    finally:
        cursor.close()
        connection.close()

    if user_id is not None:
        prune_superseded_cookies(user_id)

    return not failed


def _store_cookie_rows(cursor: MySQLCursorAbstract, cookies: list[dict],
                       user_id: Optional[int]) -> list[str]:
    """Insert/update each cookie row; returns the names of the ones that could NOT be stored.

    Issue #745: a row with no user_id cannot be bound by the AAD, so encrypt_secret would store the
    value as PLAINTEXT — and get_cookies JOINs users, so nothing could ever read it back. That
    leaves a live li_at in the clear which encrypt_secrets_at_rest (scanning user-owned rows) would
    never find. Refuse the write instead of creating an unreadable plaintext credential.
    """
    if user_id is None:
        log_error("Refusing to store cookies with no user_id — the row could not be bound to a "
                  "user, so it would be a plaintext session no read path can return")
        return [str(cookie.get('name')) for cookie in cookies]

    failed: list[str] = []
    for cookie in cookies:
        try:
            cursor.execute("""
                INSERT INTO cookies (name, value, domain, path, expiry, secure, http_only, user_id)
                VALUES (%s, %s, %s, %s, FROM_UNIXTIME(%s), %s, %s, %s)
                ON DUPLICATE KEY UPDATE
                    value = VALUES(value),
                    path = VALUES(path),
                    expiry = VALUES(expiry),
                    secure = VALUES(secure),
                    http_only = VALUES(http_only)
               
            """, (

                cookie['name'],
                encrypt_secret(cookie['value'], user_id, SECRET_FIELD_COOKIE_VALUE),
                cookie['domain'],
                cookie['path'],
                cookie['expiry'] if 'expiry' in cookie else None,
                cookie['secure'],
                cookie['httpOnly'],
                user_id
            ))
        except mysql.connector.Error as err:
            myprint(f"Could not add cookie to database | Error: {err}")
            failed.append(str(cookie.get('name')))
    return failed


def prune_superseded_cookies(user_id: int) -> int:
    """Keep only the most-recently-updated row per (user_id, name), deleting older duplicates
    left behind when the same cookie is re-stored under a different domain scope — e.g. the
    extension writes li_at on '.linkedin.com' while a prior Selenium login stored it on
    '.www.linkedin.com'. get_cookies matches on `domain LIKE %tld%`, so a stale variant would
    otherwise be returned alongside the fresh one and could shadow it at login.

    Conservative by design: it only deletes a row when a STRICTLY newer sibling of the same
    name exists for the same user, so it never removes the newest copy and never touches a
    uniquely-named cookie. Best-effort — a failure here never breaks the cookie write.
    """
    connection = _connection.get_db_connection()
    cursor = connection.cursor()
    deleted = 0
    try:
        cursor.execute("""
            DELETE older
            FROM cookies older
            JOIN cookies newer
              ON older.user_id = newer.user_id
             AND older.name = newer.name
             AND (newer.updated_at > older.updated_at
                  OR (newer.updated_at = older.updated_at AND newer.id > older.id))
            WHERE older.user_id = %s
        """, (user_id,))
        deleted = cursor.rowcount
        connection.commit()
        if deleted:
            myprint(f"Pruned {deleted} superseded cookie(s) for user_id {user_id}")
    except mysql.connector.Error as err:
        myprint(f"Could not prune superseded cookies for user_id {user_id} | Error: {err}")
    finally:
        cursor.close()
        connection.close()
    return deleted


def get_cookies(url: str, user_email: str):
    """Selenium-ready cookie dicts for `url`'s top-level domain, for one user's stored session.

    `user_id` is selected only to unseal the row and is popped before returning — `add_cookie()` rejects
    any key it does not know. A cookie whose value would not decrypt is DROPPED rather than handed back
    empty: an empty `li_at` would install a dead session that LEM then reports as signed in.

    Returns None (not []) when the query itself failed, so "nothing stored" and "could not read the
    cookie table" stay distinguishable.
    """
    connection = _connection.get_db_connection()
    cursor = connection.cursor(dictionary=True)

    # Extract the top-level domain from the URL
    tld = get_top_level_domain(url)

    try:
        cursor.execute("""
            SELECT c.name, c.value, c.domain, c.path, UNIX_TIMESTAMP(c.expiry) AS expiry, c.secure,
                   c.http_only, c.user_id
            FROM cookies c
            JOIN users u ON c.user_id = u.id
            WHERE c.domain LIKE %s AND u.email = %s
        """, (f"%{tld}%", user_email))

        cookies = cursor.fetchall()
        for cookie in cookies or []:
            # user_id is selected only to unseal the row — Selenium's add_cookie() rejects any
            # key it doesn't know, so it must not survive into the returned dict.
            cookie['value'] = decrypt_secret(
                cookie['value'], cookie.pop('user_id', None), SECRET_FIELD_COOKIE_VALUE)
        # A cookie that could not be decrypted is worse than a missing one: Selenium would set an
        # empty li_at and LEM would report "logged in" against a dead session.
        cookies = [c for c in (cookies or []) if c.get('value')]
    except mysql.connector.Error as err:
        myprint(f"Could not get cookies from DB | Error: {err}")
        cookies = None
    finally:
        cursor.close()
        connection.close()

    return cookies


def store_linkedin_li_at(user_id: int, li_at: str, jsessionid: Optional[str] = None) -> bool:
    """Persist a user-supplied LinkedIn session cookie (li_at, optionally JSESSIONID).

    Lets login_to_linkedin resume an already-trusted session instead of doing a fresh
    password login — which is what triggers LinkedIn's new-device challenge. Reuses the
    standard cookie store so the existing cookie-first login path picks it up.
    """
    email = get_user_email(user_id)
    if not email:
        myprint(f"store_linkedin_li_at: no email for user_id {user_id}")
        return False

    import time
    expiry = int(time.time()) + 365 * 24 * 60 * 60  # ~1 year; load_cookies re-stamps anyway
    cookies = [{
        "name": "li_at", "value": li_at, "domain": ".linkedin.com", "path": "/",
        "expiry": expiry, "secure": True, "httpOnly": True,
    }]
    if jsessionid:
        cookies.append({
            "name": "JSESSIONID", "value": jsessionid, "domain": ".linkedin.com",
            "path": "/", "expiry": expiry, "secure": True, "httpOnly": False,
        })
    try:
        # Must reflect the actual write (issue #745): the caller drops the user's stored LinkedIn
        # password on a True, and per-row insert errors are swallowed inside _store_cookie_rows.
        if not store_cookies(email, cookies):
            log_error("Could not store LinkedIn session cookie — no row was written",
                      user_id=user_id)
            return False
        return True
    except Exception as e:
        myprint(f"Could not store LinkedIn session cookie for user_id {user_id}: {e}")
        return False


def has_linkedin_session(user_id: int) -> bool:
    """True if the user has a stored LinkedIn session cookie (li_at) to log in with."""
    try:
        with db_cursor() as cursor:
            cursor.execute(
                "SELECT 1 FROM cookies WHERE user_id = %s AND name = 'li_at' LIMIT 1",
                (user_id,),
            )
            return cursor.fetchone() is not None
    except mysql.connector.Error as err:
        myprint(f"Could not check linkedin session for user_id {user_id} | Error: {err}")
        return False


def get_linkedin_session_email_sent_at(user_id: int):
    """Return the datetime the last session notification email was sent, or None."""
    try:
        with db_cursor() as cursor:
            cursor.execute(
                "SELECT linkedin_session_email_sent_at FROM users WHERE id = %s", (user_id,)
            )
            row = cursor.fetchone()
            return row[0] if row else None
    except mysql.connector.Error as err:
        myprint(f"Could not read session email timestamp for user_id {user_id} | Error: {err}")
        return None


def set_linkedin_session_email_sent_at(user_id: int) -> bool:
    """Stamp now() as the last session notification email time (throttle)."""
    try:
        with db_cursor(commit=True) as cursor:
            cursor.execute(
                "UPDATE users SET linkedin_session_email_sent_at = NOW() WHERE id = %s", (user_id,)
            )
            return True
    except mysql.connector.Error as err:
        myprint(f"Could not set session email timestamp for user_id {user_id} | Error: {err}")
        return False


def add_user(email: str, password: str):
    """Create a user from an email + password, sealing the password against the id the INSERT allocates.

    Two statements on purpose: the ciphertext is bound to `users.id` as AAD, which auto-increment only
    hands out once the row exists. A duplicate email is logged and swallowed, and nothing is returned
    either way — the caller learns the outcome by looking the user up.
    """
    try:
        with db_cursor(commit=True) as cursor:
        # The row has to exist before the password can be encrypted — the ciphertext is bound to
        # users.id (AAD), which auto-increment only hands out on INSERT.
            cursor.execute("INSERT INTO users (email) VALUES (%s)", (email,))
            user_id = cursor.lastrowid
            cursor.execute("UPDATE users SET password = %s WHERE id = %s",
                           (encrypt_secret(password, user_id, SECRET_FIELD_PASSWORD), user_id))
    except mysql.connector.Error as e:
        if e.errno == errorcode.ER_DUP_ENTRY:
            myprint(f"User with email {email} already exists.")
        else:
            myprint(f"An error occurred: {e}")


def add_user_with_access_token(email: str, linked_sub_id: str, access_token: str, access_token_expires_in: str,
                               refresh_token: str = None,
                               refresh_token_expires_in: str = None):
    """Upsert a user from a LinkedIn OAuth callback and store the sealed tokens.

    Split into an identity upsert and a token UPDATE because the tokens are sealed against `users.id`,
    which does not exist yet for a brand-new user. On the ON DUPLICATE KEY branch MySQL does not report
    the existing row's id in `lastrowid` — it can hand back the auto-increment value the failed insert
    allocated and burned — so `id = LAST_INSERT_ID(id)` pins it and an email lookup backs that up.
    Sealing against the wrong id stores a token bound to a row that does not exist, which is
    indistinguishable from storing no token at all.

    Errors are logged, not raised, and nothing is returned either way.
    """
    connection = _connection.get_db_connection()
    cursor = connection.cursor()

    access_token_created_at = datetime.now(timezone.utc)

    if refresh_token is not None:
        refresh_token_created_at = datetime.now(timezone.utc)
    else:
        refresh_token_created_at = None

    try:
        # Two statements on purpose (issue #745): the OAuth tokens are encrypted under a key
        # derived from users.id, which does not exist yet for a brand-new user. Upsert the
        # non-secret identity columns first, then write the ciphertext against the settled id.
        # `id = LAST_INSERT_ID(id)` is load-bearing: on the ON DUPLICATE KEY UPDATE branch MySQL
        # does NOT report the existing row's id in lastrowid — it can hand back the auto-increment
        # value that was allocated and burned by the failed insert. Sealing the tokens against that
        # id would bind them to a row that does not exist (silently storing no token at all), so
        # the id is pinned explicitly here and still cross-checked by email below.
        cursor.execute("""INSERT INTO users (email, linked_sub_id, last_login, linkedin_connection_status)
        VALUES (%s, %s, %s, 'connected')
        ON DUPLICATE KEY UPDATE
                id = LAST_INSERT_ID(id),
                linked_sub_id = VALUES(linked_sub_id),
                last_login = VALUES(last_login),
                linkedin_connection_status = 'connected'
        """, (email, linked_sub_id, datetime.now(timezone.utc)))
        user_id = cursor.lastrowid
        if not user_id:
            cursor.execute("SELECT id FROM users WHERE email = %s", (email,))
            row = cursor.fetchone()
            user_id = row[0] if row else None
        if user_id is None:
            raise mysql.connector.Error(f"Could not resolve user id for {email}")
        cursor.execute("""UPDATE users SET
                access_token = %s,
                access_token_expires_in = %s,
                access_token_created_at = %s,
                refresh_token = %s,
                refresh_token_expires_in = %s,
                refresh_token_created_at = %s
            WHERE id = %s""", (
            encrypt_secret(access_token, user_id, SECRET_FIELD_ACCESS_TOKEN),
            access_token_expires_in, access_token_created_at,
            encrypt_secret(refresh_token, user_id, SECRET_FIELD_REFRESH_TOKEN),
            refresh_token_expires_in, refresh_token_created_at,
            user_id))
        connection.commit()
    except mysql.connector.Error as e:
        if e.errno == errorcode.ER_DUP_ENTRY:
            myprint(f"User with email {email} already exists.")
        else:
            myprint(f"An error occurred: {e}")
    finally:
        cursor.close()
        connection.close()


def get_user_linked_sub_id(user_id: int):
    """The LinkedIn OAuth subject id stored for this user.

    None covers both "no such user" and a failed read.
    """
    try:
        with db_cursor(dictionary=True) as cursor:
            cursor.execute("SELECT linked_sub_id FROM users WHERE id = %s", (user_id,))

            linked_sub_id = cursor.fetchone()
    except mysql.connector.Error as err:
        myprint(f"Could not get user linked sub id | Error: {err}")
        linked_sub_id = None

    return linked_sub_id['linked_sub_id'] if linked_sub_id else None


def get_user_access_token(user_id: int):
    """The user's decrypted LinkedIn access token, or None when it is missing, expired or unreadable.

    Expiry is evaluated in SQL against the database's own NOW(), so a lapsed token reads as ABSENT rather
    than as a token that will 401 later; a row with no recorded created_at/expires_in is treated as still
    valid. A token that will not decrypt also comes back None.
    """
    try:
        with db_cursor(dictionary=True) as cursor:
            cursor.execute(
                "SELECT access_token FROM users WHERE id = %s AND ("
                "access_token_created_at IS NULL "
                "OR access_token_expires_in IS NULL "
                "OR DATE_ADD(access_token_created_at, INTERVAL access_token_expires_in SECOND) > NOW()"
                ")",
                (user_id,),
            )

            access_token = cursor.fetchone()
    except mysql.connector.Error as err:
        myprint(f"Could not get user access token | Error: {err}")
        access_token = None

    if not access_token:
        return None
    return decrypt_secret(access_token['access_token'], user_id, SECRET_FIELD_ACCESS_TOKEN)


def get_user_id(email: str):
    """Resolve an email address to a user id.

    None conflates "no such address" with "the lookup failed", so it is never on its own proof that an
    account does not exist.
    """
    try:
        with db_cursor(dictionary=True) as cursor:
            cursor.execute("SELECT id FROM users WHERE email = %s", (email,))

            user_id = cursor.fetchone()
    except mysql.connector.Error as err:
        myprint(f"Could not get user id | Error: {err}")
        user_id = None

    return user_id['id'] if user_id else None


def insert_post(email: str, content: str, scheduled_time: datetime, post_type: PostType,
                video_url: Optional[str] = None, carousel_slides: Optional[list[str]] = None,
                video_quality: str = "standard", status: PostStatus = PostStatus.PENDING,
                use_avatar: Optional[bool] = None, image_url: Optional[str] = None) -> bool:
    """Insert a fully-formed post for the account behind `email`.

    `use_avatar` is deliberately three-valued: NULL means the composer expressed no preference for this
    post, so the per-user opt-ins decide (issue #744); 0/1 is an explicit compose-time choice. An unknown
    email is logged and returns False rather than raising.
    """
    user_id = get_user_id(email)

    success = False

    if not user_id:
        myprint(f"User with email {email} not found.")
        return success

    try:
        with db_cursor(commit=True) as cursor:
            scheduled_time = to_naive_utc(scheduled_time)

            slides_json = json.dumps(carousel_slides) if carousel_slides else None

            # use_avatar is deliberately three-valued: NULL = the user expressed no preference for this
            # post, so the per-user opt-ins decide (issue #744). 0/1 is an explicit compose-time choice.
            cursor.execute("""
                INSERT INTO posts (content, scheduled_time, post_type, user_id, video_url, carousel_slides, video_quality, status, use_avatar, image_url)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """, (content, scheduled_time, post_type.value, user_id, video_url, slides_json,
                  video_quality or "standard", status.value,
                  None if use_avatar is None else int(bool(use_avatar)), image_url))

            success = cursor.rowcount == 1
    except mysql.connector.Error as e:
        success = False
        myprint(f"Count not insert post. An error occurred: {e}")

    return success


def insert_planned_post(user_id: int, scheduled_time: datetime, post_type: PostType, buyer_stage: str,
                        content_mix: Optional[str] = None) -> bool:
    """Insert the SKELETON of a planned post — schedule slot, type, buyer stage and mix class, no content.

    Lands at `PostStatus.PLANNING` with the literal body 'TBD', which is the placeholder the generation
    pass overwrites later.
    """
    connection = _connection.get_db_connection()
    cursor = connection.cursor()

    success = False

    try:
        scheduled_time = to_naive_utc(scheduled_time)

        cursor.execute("""
            INSERT INTO posts (scheduled_time, post_type, user_id, buyer_stage, content_mix, status, content)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
        """, (scheduled_time, post_type.value, user_id, buyer_stage,
              str(content_mix) if content_mix else None, PostStatus.PLANNING.value, 'TBD'))

        connection.commit()
        success = cursor.rowcount == 1
    except mysql.connector.Error as e:
        success = False
        myprint(f"Count not insert planned post. An error occurred: {e}")
    finally:
        cursor.close()
        connection.close()
    return success


def insert_occasion_post(user_id: int, scheduled_time: datetime, buyer_stage: str) -> Optional[int]:
    """Insert the SKELETON of an occasion/milestone post and return its id (issue #1074).

    Lands at `PostStatus.PLANNING` with the 'TBD' placeholder, exactly like `insert_planned_post` —
    the drafting task overwrites it — but with `manual_publish = 1`, which is what permanently keeps
    the scheduler and `post_to_linkedin` off the row. The id comes back because the caller has to
    hand it to the drafting task; None means nothing was written.
    """
    connection = _connection.get_db_connection()
    cursor = connection.cursor()

    post_id = None
    try:
        cursor.execute("""
            INSERT INTO posts (scheduled_time, post_type, user_id, buyer_stage, status, content,
                               manual_publish)
            VALUES (%s, %s, %s, %s, %s, %s, 1)
        """, (to_naive_utc(scheduled_time), PostType.TEXT.value, user_id, buyer_stage,
              PostStatus.PLANNING.value, 'TBD'))
        connection.commit()
        post_id = cursor.lastrowid if cursor.rowcount == 1 else None
    except mysql.connector.Error as e:
        log_error("Could not insert occasion post", exc=e, user_id=user_id)
    finally:
        cursor.close()
        connection.close()
    return post_id


def get_post_manual_publish(post_id: int) -> bool:
    """True when this post publishes by hand through LinkedIn's native occasion composer (#1074).

    Fails CLOSED-ish in the direction that matters: an unreadable row answers False, which is the
    pre-#1074 behaviour for every post that ever existed — the automatic path. The scheduler query
    is the primary gate; this is the publish-time cross-check.
    """
    connection = _connection.get_db_connection()
    cursor = connection.cursor()
    try:
        cursor.execute("SELECT manual_publish FROM posts WHERE id = %s", (post_id,))
        row = cursor.fetchone()
    except mysql.connector.Error as err:
        myprint(f"Could not read manual_publish for post {post_id} | Error: {err}")
        row = None
    finally:
        cursor.close()
        connection.close()
    return bool(row[0]) if row else False


def update_db_post(content: str, video_url: str, scheduled_time: datetime, post_type: PostType, post_id: int,
                   post_status: PostStatus, user_id: Optional[int] = None) -> bool:
    """`user_id` scopes the write to one account's row — same reason as `bulk_update_posts`."""
    connection = _connection.get_db_connection()
    cursor = connection.cursor()

    success = False

    try:

        scheduled_time = to_naive_utc(scheduled_time)

        params: list = [content, video_url, scheduled_time, post_type.value, post_status.value, post_id]
        owner_clause = ""
        if user_id is not None:
            owner_clause = " AND user_id = %s"
            params.append(user_id)

        cursor.execute(
            "UPDATE posts SET content = %s, video_url = %s, scheduled_time =%s, post_type = %s, "
            f"status = %s WHERE id = %s{owner_clause}",
            params
        )

        connection.commit()
        success = cursor.rowcount == 1
    except mysql.connector.Error as e:
        success = False
        myprint(f"Count not update post. An error occurred: {e}")
    finally:
        cursor.close()
        connection.close()

    return success


def update_db_post_content(post_id: int, content: str) -> bool:
    """Overwrite a post's body.

    False means the row was not CHANGED, which is three different facts: the write failed, no row
    matched (this never creates a post), or the row already held this exact content. MySQL reports
    changed rather than matched rows unless the connection sets `CLIENT.FOUND_ROWS`, and
    `_get_mysql_config` does not — so re-saving identical content answers False.
    """
    try:
        with db_cursor(commit=True) as cursor:
            cursor.execute(
                "UPDATE posts SET content = %s WHERE id = %s",
                (content, post_id)
            )

            success = cursor.rowcount == 1
    except mysql.connector.Error as e:
        success = False
        myprint(f"Count not update post content. An error occurred: {e}")

    return success


def update_db_post_video_url(post_id: int, video_url: str) -> bool:
    """Point a post at its rendered video.

    False when the write failed or no row matched.
    """
    try:
        with db_cursor(commit=True) as cursor:
            cursor.execute(
                "UPDATE posts SET video_url = %s WHERE id = %s",
                (video_url, post_id)
            )

            success = cursor.rowcount == 1
    except mysql.connector.Error as e:
        success = False
        myprint(f"Count not update post video url. An error occurred: {e}")

    return success


def update_db_post_status(post_id: int, post_status: PostStatus) -> bool:
    """Move a post to `post_status`.

    The MySQL connector cannot bind a StrEnum, so the `.value` is read first — and that read is wrapped:
    anything without a `.value` (a bare string, say) leaves the fallback in place and the post is written
    as 'posted'. Pass a real `PostStatus`.

    False means the row was not CHANGED, not that the write failed: setting a post to the status it
    already holds answers False, because the connection does not set `CLIENT.FOUND_ROWS` and MySQL
    therefore counts changed rather than matched rows.
    """
    connection = _connection.get_db_connection()
    cursor = connection.cursor()

    # The MySQL connector can't bind a PostStatus enum directly — it binds the .value string.
    status_str = "posted"
    try:
        status_str = post_status.value
    except Exception:
        myprint(f"Error converting post_status to string: {post_status}")

    try:
        cursor.execute(
            """UPDATE posts SET status = %s WHERE id = %s""",
            (status_str, post_id)
        )

        connection.commit()
        success = cursor.rowcount == 1
    except mysql.connector.Error as e:
        success = False
        myprint(f"Count not update post status. An error occurred: {e}")
    finally:
        cursor.close()
        connection.close()

    return success


# Columns the Review & Edit list may be ordered by. Whitelisted to keep the
# ORDER BY clause injection-safe (the value is interpolated, not parameterized).
_POST_SORT_COLUMNS = {
    'scheduled_time': 'scheduled_time',
    'status': 'status',
    'post_type': 'post_type',
    'id': 'id',
}

_SEARCH_MAX_TERMS = 20


def _escape_like(term: str) -> str:
    """Escape LIKE wildcards so a search term matches literally (default '\\' escape)."""
    return term.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')


def build_content_search_clause(search: Optional[str], column: str = 'content') -> tuple[Optional[str], list]:
    """Parse a boolean keyword query into a parameterized SQL condition over *column*.

    Supports AND / OR / NOT (case-insensitive), implicit AND between adjacent terms,
    parentheses for grouping, and "quoted phrases". Each bare term matches the column
    case-insensitively via LIKE %term% (wildcards in the term are escaped). Returns
    ``(sql_fragment, params)`` — the fragment is already fully parenthesized and safe
    to drop into a WHERE — or ``(None, [])`` when the query is empty. Unparseable input
    falls back to a single LIKE over the raw string so search never hard-errors.
    """
    if not search or not search.strip():
        return None, []

    # ── Tokenize: parens, "quoted phrases", and bare words (AND/OR/NOT are keywords).
    tokens: list[tuple[str, str]] = []
    i, n = 0, len(search)
    while i < n:
        ch = search[i]
        if ch.isspace():
            i += 1
        elif ch == '(':
            tokens.append(('LPAREN', ch)); i += 1
        elif ch == ')':
            tokens.append(('RPAREN', ch)); i += 1
        elif ch == '"':
            j = i + 1
            while j < n and search[j] != '"':
                j += 1
            phrase = search[i + 1:j]
            if phrase.strip():
                tokens.append(('TERM', phrase.strip()))
            i = j + 1
        else:
            j = i
            while j < n and not search[j].isspace() and search[j] not in '()"':
                j += 1
            word = search[i:j]
            upper = word.upper()
            if upper in ('AND', 'OR', 'NOT'):
                tokens.append((upper, word))
            else:
                tokens.append(('TERM', word))
            i = j

    if not tokens:
        return None, []

    params: list = []
    term_count = 0

    class _ParseError(Exception):
        pass

    pos = 0

    def _peek() -> Optional[str]:
        return tokens[pos][0] if pos < len(tokens) else None

    def _term_sql(value: str) -> str:
        nonlocal term_count
        term_count += 1
        if term_count > _SEARCH_MAX_TERMS:
            raise _ParseError('too many terms')
        params.append(f"%{_escape_like(value)}%")
        return f"{column} LIKE %s"

    def _parse_or() -> str:
        node = _parse_and()
        while _peek() == 'OR':
            nonlocal_advance()
            node = f"({node} OR {_parse_and()})"
        return node

    def _parse_and() -> str:
        node = _parse_not()
        while _peek() in ('AND', 'NOT', 'TERM', 'LPAREN'):
            if _peek() == 'AND':
                nonlocal_advance()
            node = f"({node} AND {_parse_not()})"
        return node

    def _parse_not() -> str:
        if _peek() == 'NOT':
            nonlocal_advance()
            return f"(NOT {_parse_not()})"
        return _parse_atom()

    def _parse_atom() -> str:
        tok = _peek()
        if tok == 'LPAREN':
            nonlocal_advance()
            inner = _parse_or()
            if _peek() != 'RPAREN':
                raise _ParseError('unbalanced parens')
            nonlocal_advance()
            return f"({inner})"
        if tok == 'TERM':
            value = tokens[pos][1]
            nonlocal_advance()
            return _term_sql(value)
        raise _ParseError(f'unexpected token {tok}')

    def nonlocal_advance() -> None:
        nonlocal pos
        pos += 1

    try:
        sql = _parse_or()
        if pos != len(tokens):
            raise _ParseError('trailing tokens')
        return sql, params
    except _ParseError:
        # Fallback: treat the whole raw query as one literal term.
        return f"{column} LIKE %s", [f"%{_escape_like(search.strip())}%"]


def get_posts(user_id: int, limit: int = 10, offset: int = 0,
              sort_order: str = 'asc', status_filter: Optional[str] = None,
              post_type_filter: Optional[str] = None, search: Optional[str] = None,
              sort_by: str = 'scheduled_time',
              start_date: Optional[datetime] = None,
              end_date: Optional[datetime] = None) -> tuple[list, int]:
    """One page of a user's posts plus the TOTAL number matching, for the Review & Edit list.

    The count runs over the same WHERE clause as the page, so pagination stays honest under filtering.
    `sort_by` is whitelisted through `_POST_SORT_COLUMNS` (anything unknown falls back to
    `scheduled_time`) because the column name is interpolated rather than parameterized; `search` becomes
    a quoted-term / AND-OR clause via `build_content_search_clause`. Date bounds are coerced to naive UTC.

    A read error returns `([], 0)` — an empty page, never a partial one.
    """
    connection = _connection.get_db_connection()
    cursor = connection.cursor(dictionary=True)

    order = 'ASC' if sort_order.lower() != 'desc' else 'DESC'
    sort_col = _POST_SORT_COLUMNS.get((sort_by or '').lower(), 'scheduled_time')

    try:
        where = "WHERE user_id = %s"
        params: list = [user_id]
        if status_filter:
            where += " AND status = %s"
            params.append(status_filter.lower())
        if post_type_filter:
            where += " AND post_type = %s"
            params.append(post_type_filter.lower())
        if start_date is not None:
            where += " AND scheduled_time >= %s"
            params.append(to_naive_utc(start_date))
        if end_date is not None:
            where += " AND scheduled_time <= %s"
            params.append(to_naive_utc(end_date))
        search_sql, search_params = build_content_search_clause(search)
        if search_sql:
            where += f" AND ({search_sql})"
            params.extend(search_params)

        cursor.execute(
            f"SELECT COUNT(*) AS total FROM posts {where}",
            params
        )
        total = cursor.fetchone()['total']

        cursor.execute(
            f"SELECT id, content, video_url, image_url, scheduled_time, post_type, status, "
            f"carousel_slides, authenticity_score, gate_reason, rejection_reason, archetype, "
            f"manual_publish "
            f"FROM posts {where} ORDER BY {sort_col} {order}, id {order} LIMIT %s OFFSET %s",
            params + [limit, offset]
        )
        posts = cursor.fetchall()
    except mysql.connector.Error as err:
        myprint(f"Could not get posts for user id: {user_id} | Error: {err}")
        posts = []
        total = 0
    finally:
        cursor.close()
        connection.close()

    return posts, total


def get_dashboard_counts(user_id: int, week_start) -> dict:
    """Dashboard top-line counts via SQL aggregates over ALL of the user's posts. Replaces the old
    approach of counting in Python over get_posts()'s 10-oldest-posts slice (which made 'posted'
    cap near 10 and 'scheduled this week' read ~0). week_start is coerced to a naive UTC datetime so
    it compares cleanly against the naive UTC scheduled_time column (no tz TypeError).
    """
    if week_start is not None and getattr(week_start, "tzinfo", None) is not None:
        week_start = week_start.astimezone(timezone.utc).replace(tzinfo=None)
    try:
        with db_cursor() as cursor:
            cursor.execute(
                "SELECT "
                "  COALESCE(SUM(status IN (%s,%s) AND scheduled_time >= %s), 0) AS scheduled_this_week, "
                "  COALESCE(SUM(status = %s), 0) AS pending_review, "
                "  COALESCE(SUM(status = %s), 0) AS posted_total "
                "FROM posts WHERE user_id = %s",
                (PostStatus.APPROVED.value, PostStatus.PENDING.value, week_start,
                 PostStatus.PENDING.value, PostStatus.POSTED.value, user_id))
            row = cursor.fetchone()
            return {"scheduled_this_week": int(row[0] or 0),
                    "pending_review": int(row[1] or 0),
                    "posted_total": int(row[2] or 0)}
    except mysql.connector.Error as err:
        myprint(f"Could not get dashboard counts for user {user_id} | Error: {err}")
        return {"scheduled_this_week": 0, "pending_review": 0, "posted_total": 0}


def get_planned_tasks(user_id: int, limit: int = 10) -> list[dict]:
    """Upcoming (future-dated, non-terminal) work for the dashboard "Planned Tasks" card:
    scheduled/approved/pending POSTS, scheduled DMs, and upcoming NEWSLETTER editions — each
    labeled by `kind` (Post / DM / Newsletter). Terminal states (posted/sent/published/etc.)
    are excluded, results are merged and sorted soonest-first, capped at `limit`.
    """
    connection = _connection.get_db_connection()
    cursor = connection.cursor(dictionary=True)
    tasks: list[dict] = []
    try:
        cursor.execute(
            "SELECT id, content, scheduled_time, status FROM posts "
            "WHERE user_id = %s AND status IN (%s, %s, %s) "
            "AND scheduled_time >= UTC_TIMESTAMP() ORDER BY scheduled_time ASC LIMIT %s",
            (user_id, PostStatus.PENDING.value, PostStatus.APPROVED.value,
             PostStatus.SCHEDULED.value, limit))
        for row in cursor.fetchall():
            tasks.append({
                "kind": "Post",
                "id": row["id"],
                "title": (row.get("content") or "").strip()[:120] or "Scheduled post",
                "scheduled_time": row["scheduled_time"],
                "status": row["status"],
            })

        cursor.execute(
            "SELECT id, recipient_name, message, scheduled_time, status FROM scheduled_dms "
            "WHERE user_id = %s AND status IN (%s, %s, %s) "
            "AND scheduled_time >= UTC_TIMESTAMP() ORDER BY scheduled_time ASC LIMIT %s",
            (user_id, ScheduledDmStatus.PENDING.value, ScheduledDmStatus.APPROVED.value,
             ScheduledDmStatus.SCHEDULED.value, limit))
        for row in cursor.fetchall():
            title = (row.get("recipient_name") or "").strip() or (row.get("message") or "").strip()[:120]
            tasks.append({
                "kind": "DM",
                "id": row["id"],
                "title": title or "Scheduled DM",
                "scheduled_time": row["scheduled_time"],
                "status": row["status"],
            })

        # newsletter_editions has no status enum in code; 'draft'/'approved' are the non-terminal
        # states (mirrors get_pending_newsletter_editions), 'published'/'failed'/'skipped' terminal.
        cursor.execute(
            "SELECT id, title, scheduled_for, status FROM newsletter_editions "
            "WHERE user_id = %s AND status IN ('draft', 'approved') "
            "AND scheduled_for >= UTC_TIMESTAMP() ORDER BY scheduled_for ASC LIMIT %s",
            (user_id, limit))
        for row in cursor.fetchall():
            tasks.append({
                "kind": "Newsletter",
                "id": row["id"],
                "title": (row.get("title") or "").strip() or "Newsletter edition",
                "scheduled_time": row["scheduled_for"],
                "status": row["status"],
            })
    except mysql.connector.Error as err:
        myprint(f"Could not get planned tasks for user {user_id} | Error: {err}")
        return []
    finally:
        cursor.close()
        connection.close()

    tasks.sort(key=lambda t: t["scheduled_time"])
    return tasks[:limit]


def get_default_video_quality(user_id: int) -> str:
    """The user's preferred default video quality for AUTO-generated posts (engagement_preferences).
    Falls back to 'standard' when unset/invalid — premium is only ever honored when credits exist,
    which is enforced separately at render time.
    """
    try:
        with db_cursor(dictionary=True) as cursor:
            cursor.execute(
                "SELECT default_video_quality FROM engagement_preferences WHERE user_id = %s",
                (user_id,))
            row = cursor.fetchone()
            quality = row.get("default_video_quality") if row else None
            return quality if quality in VALID_VIDEO_QUALITIES else "standard"
    except mysql.connector.Error as err:
        myprint(f"Could not get default video quality for user {user_id} | Error: {err}")
        return "standard"


def set_default_video_quality(user_id: int, quality: str) -> bool:
    """Set the user's default video quality preference (upserts the engagement_preferences row).
    Invalid values are coerced to 'standard'.
    """
    if quality not in VALID_VIDEO_QUALITIES:
        quality = "standard"
    return update_engagement_preferences(user_id, {"default_video_quality": quality})


def get_posted_posts(user_id: int):
    """Every post this user actually published, oldest first.

    None (not []) when the read failed.
    """
    try:
        with db_cursor(dictionary=True) as cursor:
            cursor.execute(
                "SELECT id, content, scheduled_time, post_type, status FROM posts WHERE user_id = %s AND status = 'posted' ORDER BY scheduled_time asc",
                (user_id,))

            posts = cursor.fetchall()
    except mysql.connector.Error as err:
        myprint(f"Could not get posted posts for user id: {user_id} | Error: {err}")
        posts = None

    return posts


# `get_post_by_email` lived here until issue #914. It turned an ADDRESS into somebody's posts, which
# is exactly the shape `GET /posts/` used to authenticate on; its one caller now resolves the caller
# from the session and calls `get_posts(user_id, …)` directly. Leaving the wrapper behind would keep
# an address-keyed reader one import away from the next endpoint — deleted rather than deprecated.


def get_post_content(post_id: int):
    """A post's body text, or None when the post does not exist or the read failed."""
    try:
        with db_cursor(dictionary=True) as cursor:
            cursor.execute("SELECT content FROM posts WHERE id = %s", (post_id,))

            post = cursor.fetchone()
    except mysql.connector.Error as err:
        myprint(f"Could not get post content for post id: {post_id} | Error: {err}")
        post = False

    return post['content'] if post else None


def get_post_user_id(post_id: int):
    """Who owns a post.

    None conflates "no such post" with a failed read, so this is not by itself an authorisation answer —
    `user_owns_posts` is the fail-closed one (issue #914).
    """
    try:
        with db_cursor(dictionary=True) as cursor:
            cursor.execute("SELECT user_id FROM posts WHERE id = %s", (post_id,))

            post = cursor.fetchone()
    except mysql.connector.Error as err:
        myprint(f"Could not get post user id for post id: {post_id} | Error: {err}")
        post = False

    return post['user_id'] if post else None




def user_owns_posts(user_id: int, post_ids: list[int]) -> bool:
    """True only when EVERY id exists AND belongs to `user_id` (issue #914).

    The post-mutating endpoints take a list of ids and used to act on it unchecked, so this is the
    authorisation read that stands between one account and another's drafts. It fails CLOSED: an
    empty list and a missing row both answer False, because "we could not prove ownership" must
    never be spelled the same way as "they own it". A database error raises `OwnershipUnprovable`
    rather than answering False — still a refusal at the call site, but a truthful one.
    """
    if not user_id or not post_ids:
        return False

    unique_ids = list({int(pid) for pid in post_ids})
    try:
        with db_cursor() as cursor:
            placeholders = ', '.join(['%s'] * len(unique_ids))
            cursor.execute(
                f"SELECT COUNT(DISTINCT id) FROM posts WHERE user_id = %s AND id IN ({placeholders})",
                [user_id, *unique_ids],
            )
            row = cursor.fetchone()
            return bool(row) and row[0] == len(unique_ids)
    except mysql.connector.Error as err:
        from cqc_lem.utilities.logger import log_error
        log_error("Could not verify post ownership", exc=err, user_id=user_id)
        raise OwnershipUnprovable(str(err)) from err


def update_db_post_image_url(post_id: int, image_url: Optional[str]) -> bool:
    """Set (or clear, with None) a post's image.

    Returns True whenever the statement ran, including when no row matched — unlike the sibling
    content/video setters, which report `rowcount`.
    """
    try:
        with db_cursor(commit=True) as cursor:
            cursor.execute(
                "UPDATE posts SET image_url = %s WHERE id = %s",
                (image_url, post_id)
            )
            return True
    except mysql.connector.Error as err:
        myprint(f"Could not update post image_url for post id: {post_id} | Error: {err}")
        return False


def get_post_image_url(post_id: int) -> Optional[str]:
    """A post's stored image path, or None when unset, absent or unreadable."""
    try:
        with db_cursor(dictionary=True) as cursor:
            cursor.execute("SELECT image_url FROM posts WHERE id = %s", (post_id,))
            post = cursor.fetchone()
    except mysql.connector.Error as err:
        myprint(f"Could not get post image_url for post id: {post_id} | Error: {err}")
        post = None

    return post['image_url'] if post else None


def get_post_video_url(post_id: int):
    """A post's stored video URL, or None when unset, absent or unreadable."""
    try:
        with db_cursor(dictionary=True) as cursor:
            cursor.execute("SELECT video_url FROM posts WHERE id = %s", (post_id,))

            post = cursor.fetchone()
    except mysql.connector.Error as err:
        myprint(f"Could not get post video_url for post id: {post_id} | Error: {err}")
        post = False

    return post['video_url'] if post else None


def get_post_buyer_stage(post_id: int) -> Optional[str]:
    """The buyer-journey stage the content plan assigned this post, or None when unset or unreadable."""
    try:
        with db_cursor(dictionary=True) as cursor:
            cursor.execute("SELECT buyer_stage FROM posts WHERE id = %s", (post_id,))
            row = cursor.fetchone()
    except mysql.connector.Error as err:
        myprint(f"Could not get buyer_stage for post id: {post_id} | Error: {err}")
        row = None
    return row['buyer_stage'] if row else None


def get_post_content_mix(post_id: int) -> Optional[str]:
    """This post's 70/20/10 mix class as assigned by the content-plan governor (issue #618).
    None for a post planned before the governor existed (or created by hand).
    """
    try:
        with db_cursor(dictionary=True) as cursor:
            cursor.execute("SELECT content_mix FROM posts WHERE id = %s", (post_id,))
            row = cursor.fetchone()
    except mysql.connector.Error as err:
        myprint(f"Could not get content_mix for post id: {post_id} | Error: {err}")
        row = None
    return row['content_mix'] if row else None


def get_content_mix_counts(user_id: int, days: Optional[int] = None) -> dict:
    """Planned/published post counts per 70/20/10 mix class for the analytics dashboard's mix-
    compliance ratio (issue #618). Rejected posts are excluded (they were never part of the mix the
    audience saw), unclassified posts are counted under 'unclassified'. `days` windows on
    scheduled_time (None = every post).
    """
    counts = {"unclassified": 0}
    try:
        with db_cursor() as cursor:
            window = "AND scheduled_time >= (NOW() - INTERVAL %s DAY) " if days is not None else ""
            params = (user_id, days) if days is not None else (user_id,)
            cursor.execute(
                "SELECT content_mix, COUNT(*) FROM posts "
                "WHERE user_id = %s AND status <> 'rejected' " + window +
                "GROUP BY content_mix", params)
            for mix, count in (cursor.fetchall() or []):
                key = str(mix).strip().lower() if mix else "unclassified"
                counts[key] = counts.get(key, 0) + int(count or 0)
    except mysql.connector.Error as err:
        myprint(f"Could not get content mix counts for user {user_id} | Error: {err}")
    return counts


def get_post_type(post_id: int) -> Optional[PostType]:
    """A post's `PostType`.

    None covers three different things on purpose: no such post, a failed read, and a stored value that
    is not a member of this build's enum — the last is what a MySQL ENUM the code has not caught up to
    looks like from here.
    """
    try:
        with db_cursor(dictionary=True) as cursor:
            cursor.execute("SELECT post_type FROM posts WHERE id = %s", (post_id,))
            row = cursor.fetchone()
    except mysql.connector.Error as err:
        myprint(f"Could not get post_type for post id: {post_id} | Error: {err}")
        row = None

    if row:
        try:
            return PostType(row['post_type'])
        except ValueError:
            return None
    return None


def get_carousel_slides(post_id: int) -> list[str]:
    """A post's carousel slide paths as a list — [] whenever there is nothing usable.

    The column holds JSON; a string is parsed, and anything that is not a list (or will not parse)
    collapses to []. Empty is always safe to iterate, so a malformed row degrades to "no slides" instead
    of raising into the poster. `get_post_carousel_slides` hands back the RAW column instead.
    """
    try:
        with db_cursor(dictionary=True) as cursor:
            cursor.execute("SELECT carousel_slides FROM posts WHERE id = %s", (post_id,))
            row = cursor.fetchone()
    except mysql.connector.Error as err:
        myprint(f"Could not get carousel_slides for post id: {post_id} | Error: {err}")
        row = None

    if row and row['carousel_slides']:
        try:
            slides = row['carousel_slides']
            if isinstance(slides, str):
                slides = json.loads(slides)
            return slides if isinstance(slides, list) else []
        except (json.JSONDecodeError, TypeError):
            return []
    return []


_ALLOWED_POST_CLAUSES = frozenset({"status = %s", "scheduled_time = %s", "rejection_reason = %s"})


def bulk_update_posts(post_ids: list[int], status: Optional[PostStatus] = None,
                      scheduled_time: Optional[datetime] = None,
                      rejection_reason: Optional[str] = None,
                      user_id: Optional[int] = None) -> bool:
    """`user_id` scopes the WHERE clause to one account's rows (issue #914).

    The API checks ownership before it calls this, so the scope is redundant today — that is the
    point. It closes the window between the check and the write, and it means a future caller that
    forgets the check cannot reach across accounts anyway.
    """
    if not post_ids:
        return False

    connection = _connection.get_db_connection()
    cursor = connection.cursor()

    success = False
    try:
        sets = []
        params: list = []

        if status is not None:
            sets.append("status = %s")
            params.append(status.value)
        if scheduled_time is not None:
            sets.append("scheduled_time = %s")
            params.append(to_naive_utc(scheduled_time))
        if rejection_reason is not None:
            sets.append("rejection_reason = %s")
            params.append((rejection_reason or "").strip() or None)

        if not sets:
            return False

        for clause in sets:
            if clause not in _ALLOWED_POST_CLAUSES:
                raise ValueError(f"Disallowed SQL clause: {clause!r}")

        placeholders = ', '.join(['%s'] * len(post_ids))
        params.extend(post_ids)

        owner_clause = ""
        if user_id is not None:
            owner_clause = " AND user_id = %s"
            params.append(user_id)

        cursor.execute(
            f"UPDATE posts SET {', '.join(sets)} WHERE id IN ({placeholders}){owner_clause}",
            params
        )
        connection.commit()
        success = cursor.rowcount > 0
    except mysql.connector.Error as e:
        from cqc_lem.utilities.logger import log_error
        log_error("Could not bulk update posts", exc=e)
        success = False
    finally:
        cursor.close()
        connection.close()

    return success


def soft_delete_posts(post_ids: list[int], rejection_reason: Optional[str] = None,
                      user_id: Optional[int] = None) -> bool:
    """Reject posts rather than delete them, so the row and its reason survive for the plan to learn from.

    `user_id` scopes the write to one account's rows — see `bulk_update_posts` for what that argument
    guarantees (issue #914).
    """
    return bulk_update_posts(post_ids, status=PostStatus.REJECTED, rejection_reason=rejection_reason,
                             user_id=user_id)


def update_db_post_rejection_reason(post_id: int, rejection_reason: Optional[str],
                                    user_id: Optional[int] = None) -> bool:
    """Persist WHY a post was rejected (issue #713) so a later regeneration can avoid the same issue.

    Empty or whitespace-only input is stored as NULL so the UI doesn't render a blank reason.
    `user_id` scopes the write to one account's row for the same reason as `bulk_update_posts`
    (issue #914) — every sibling write on this table carries it.
    """
    from cqc_lem.utilities.logger import log_error
    try:
        with db_cursor(commit=True) as cursor:
            params: list = [(rejection_reason or "").strip() or None, post_id]
            owner_clause = ""
            if user_id is not None:
                owner_clause = " AND user_id = %s"
                params.append(user_id)

            cursor.execute(
                f"UPDATE posts SET rejection_reason = %s WHERE id = %s{owner_clause}",
                params
            )
            success = cursor.rowcount == 1
    except mysql.connector.Error as e:
        success = False
        log_error(f"Could not update rejection reason for post {post_id}", exc=e, post_id=post_id)
    return success


def get_post_rejection_reason(post_id: int) -> Optional[str]:
    """The persisted rejection reason for a post (issue #713), or None when it has none."""
    try:
        with db_cursor() as cursor:
            cursor.execute("SELECT rejection_reason FROM posts WHERE id = %s", (post_id,))
            row = cursor.fetchone()
            return row[0] if row else None
    except mysql.connector.Error as err:
        from cqc_lem.utilities.logger import log_error
        log_error(f"Could not get rejection reason for post {post_id}", exc=err, post_id=post_id)
        return None


def update_db_post_carousel_slides(post_id: int, slides: list[str]) -> bool:
    """Replace a post's carousel slides, stored as JSON.

    False when the write failed or no row matched.
    """
    try:
        with db_cursor(commit=True) as cursor:
            cursor.execute(
                "UPDATE posts SET carousel_slides = %s WHERE id = %s",
                (json.dumps(slides), post_id)
            )
            success = cursor.rowcount == 1
    except mysql.connector.Error as e:
        success = False
        myprint(f"Could not update carousel_slides for post {post_id}. Error: {e}")
    return success


def update_db_post_shape(post_id: int, archetype: Optional[str], hook_style: Optional[str],
                         topic: Optional[str] = None) -> bool:
    """Persist the SHAPE (short-form archetype + hook style + topic) assigned to a generated post —
    the rotation history that keeps a user's next post from reusing a recently used shape (V51), and
    the topic attribution the feedback loop reads back off each captured stat row (#386).
    """
    try:
        with db_cursor(commit=True) as cursor:
            cursor.execute(
                "UPDATE posts SET archetype = %s, hook_style = %s, topic = %s WHERE id = %s",
                (archetype, hook_style, topic, post_id)
            )
            success = cursor.rowcount == 1
    except mysql.connector.Error as e:
        success = False
        myprint(f"Could not update shape for post {post_id}. Error: {e}")
    return success


def update_db_post_authenticity_score(post_id: int, score: Optional[int]) -> bool:
    """Persist the authenticity gate's LLM-judged score (0-100, or NULL) for a post — the reader that
    gives the previously dead post-quality column a purpose (issue #382, V57 authenticity_score). The
    content-plan status-setter reads this back to demote a low-scoring auto-approve to PENDING.
    """
    try:
        with db_cursor(commit=True) as cursor:
            cursor.execute(
                "UPDATE posts SET authenticity_score = %s WHERE id = %s",
                (score, post_id)
            )
            success = cursor.rowcount == 1
    except mysql.connector.Error as e:
        success = False
        myprint(f"Could not update authenticity score for post {post_id}. Error: {e}")
    return success


def get_post_authenticity_score(post_id: int) -> Optional[int]:
    """The authenticity gate's persisted score for a post (0-100), or None when unscored (issue #382)."""
    try:
        with db_cursor() as cursor:
            cursor.execute("SELECT authenticity_score FROM posts WHERE id = %s", (post_id,))
            row = cursor.fetchone()
            return int(row[0]) if row and row[0] is not None else None
    except mysql.connector.Error as err:
        myprint(f"Could not get authenticity score for post {post_id} | Error: {err}")
        return None


def update_db_post_gate_reason(post_id: int, findings: Optional[list]) -> bool:
    """Persist WHY a post is held for review (issue #421): the quality gates' structured findings
    (see utilities/quality_gates.py) as a JSON array on posts.gate_reason. An empty/None list clears
    the column, so a post that passes on re-score stops showing a stale reason.
    """
    try:
        with db_cursor(commit=True) as cursor:
            cursor.execute(
                "UPDATE posts SET gate_reason = %s WHERE id = %s",
                (json.dumps(findings) if findings else None, post_id)
            )
            success = cursor.rowcount == 1
    except mysql.connector.Error as e:
        success = False
        myprint(f"Could not update gate reason for post {post_id}. Error: {e}")
    return success


def get_post_gate_reason(post_id: int) -> list:
    """The persisted quality-gate findings for a post (issue #421), or [] when it has none."""
    from cqc_lem.utilities.quality_gates import parse_gate_findings
    try:
        with db_cursor() as cursor:
            cursor.execute("SELECT gate_reason FROM posts WHERE id = %s", (post_id,))
            row = cursor.fetchone()
            return parse_gate_findings(row[0] if row else None)
    except mysql.connector.Error as err:
        myprint(f"Could not get gate reason for post {post_id} | Error: {err}")
        return []


def update_db_post_dwell_score(post_id: int, score: Optional[int]) -> bool:
    """Persist the deterministic 0-100 dwell-proxy score for a post (issue #391, dwell_score column).
    Advisory metric stored next to authenticity_score — it is never read back to gate a status, so a
    failed write only costs the datapoint.
    """
    try:
        with db_cursor(commit=True) as cursor:
            cursor.execute(
                "UPDATE posts SET dwell_score = %s WHERE id = %s",
                (score, post_id)
            )
            success = cursor.rowcount == 1
    except mysql.connector.Error as e:
        success = False
        myprint(f"Could not update dwell score for post {post_id}. Error: {e}")
    return success


def get_post_dwell_score(post_id: int) -> Optional[int]:
    """The persisted dwell-proxy score for a post (0-100), or None when unscored (issue #391)."""
    try:
        with db_cursor() as cursor:
            cursor.execute("SELECT dwell_score FROM posts WHERE id = %s", (post_id,))
            row = cursor.fetchone()
            return int(row[0]) if row and row[0] is not None else None
    except mysql.connector.Error as err:
        myprint(f"Could not get dwell score for post {post_id} | Error: {err}")
        return None


def update_db_post_first_comment_link(post_id: int, link: Optional[str]) -> bool:
    """Stash the external link(s) stripped from a post body at publish time (issue #392, C3) so the
    seed-comment task can deliver them in the author's first comment. Newline-separated for multiple
    links; None clears it.
    """
    try:
        with db_cursor(commit=True) as cursor:
            cursor.execute(
                "UPDATE posts SET first_comment_link = %s WHERE id = %s",
                (link, post_id)
            )
            success = cursor.rowcount == 1
    except mysql.connector.Error as e:
        success = False
        myprint(f"Could not update first comment link for post {post_id}. Error: {e}")
    return success


def get_post_first_comment_link(post_id: int) -> Optional[str]:
    """The link(s) held back from a post's body for its first comment, or None (issue #392)."""
    try:
        with db_cursor() as cursor:
            cursor.execute("SELECT first_comment_link FROM posts WHERE id = %s", (post_id,))
            row = cursor.fetchone()
            return row[0] if row and row[0] else None
    except mysql.connector.Error as err:
        myprint(f"Could not get first comment link for post {post_id} | Error: {err}")
        return None


def get_recent_post_shape_history(user_id: int, limit: int = 10) -> list:
    """Recent posts' SHAPE history — {archetype, hook_style} dicts, most-recent first — fed to the
    shared content framework so a new post rotates away from recently used archetypes/hooks (the
    post-side twin of get_recent_newsletter_blueprint_history).
    """
    try:
        with db_cursor(dictionary=True) as cursor:
            cursor.execute(
                "SELECT archetype, hook_style FROM posts "
                "WHERE user_id = %s AND archetype IS NOT NULL "
                "ORDER BY id DESC LIMIT %s", (user_id, int(limit)))
            return cursor.fetchall()
    except mysql.connector.Error as err:
        myprint(f"Could not get post shape history for user {user_id} | Error: {err}")
        return []


def get_post_archetype(post_id: int) -> Optional[str]:
    """The short-form ARCHETYPE assigned to one post (V51 `posts.archetype`). The quality gates read
    it back so the archetype-specific checks (the no-fabrication guard on a build receipt, issue
    #619) know which contract this draft was written to.
    """
    try:
        with db_cursor() as cursor:
            cursor.execute("SELECT archetype FROM posts WHERE id = %s", (post_id,))
            row = cursor.fetchone()
            return row[0] if row else None
    except mysql.connector.Error as err:
        myprint(f"Could not get archetype for post {post_id} | Error: {err}")
        return None


def get_recent_post_texts(user_id: int, limit: int = 20,
                          exclude_post_id: Optional[int] = None) -> list:
    """Recent post CONTENT (pending/approved/posted, most-recent first) — the post-side dedup
    history (the newsletter's V49 subject dedup applied to posts). Feeds the opener/subject
    avoidance steering and the pre-persist similarity gate in create_text_post. Openers/subjects
    are derived from content on demand, so no new column is needed. `exclude_post_id` drops one post
    from the history — needed when re-scoring an ALREADY-SAVED post (issue #421), which would
    otherwise match itself at 100%.
    """
    try:
        with db_cursor() as cursor:
            exclude_sql = " AND id <> %s" if exclude_post_id is not None else ""
            params = ((user_id, exclude_post_id, int(limit)) if exclude_post_id is not None
                      else (user_id, int(limit)))
            cursor.execute(
                "SELECT content FROM posts "
                "WHERE user_id = %s AND content IS NOT NULL AND content <> '' "
                "AND status IN ('pending', 'approved', 'posted')"
                f"{exclude_sql} "
                "ORDER BY id DESC LIMIT %s", params)
            return [r[0] for r in cursor.fetchall()]
    except mysql.connector.Error as err:
        myprint(f"Could not get recent post texts for user {user_id} | Error: {err}")
        return []


def replace_video_url_base(old_base: str, new_base: str, user_id: Optional[int] = None) -> int:
    """Replace old_base URL prefix with new_base in video_url for all matching posts.

    Scoped to user_id when provided. Returns count of updated rows.
    """
    try:
        with db_cursor(commit=True) as cursor:
            if user_id is not None:
                cursor.execute(
                    "UPDATE posts SET video_url = REPLACE(video_url, %s, %s) "
                    "WHERE video_url LIKE %s AND user_id = %s",
                    (old_base, new_base, f"{old_base}%", user_id)
                )
            else:
                cursor.execute(
                    "UPDATE posts SET video_url = REPLACE(video_url, %s, %s) WHERE video_url LIKE %s",
                    (old_base, new_base, f"{old_base}%")
                )
            updated = cursor.rowcount
    except mysql.connector.Error as e:
        updated = 0
        myprint(f"Could not replace video URL base. Error: {e}")
    return updated


def get_ready_to_post_posts(pre_post_time: datetime = None, post_time_delta_minutes=20) -> list:
    """Query the database for any pending posts that are scheduled to post now or earlier.

    Answers `[]` — never None — on a read failure, because the single caller (`run_scheduler`'s
    every-10-minutes publishing beat) iterates the result directly and a None crashed it with a
    TypeError that masked the real mysql error. No post is lost by answering empty: the query's own
    24h lookback plus `get_orphaned_scheduled_posts` recover anything missed on the next tick.
    """
    now = datetime.now(timezone.utc)
    if pre_post_time is None:
        # Get time for post_time_delta after now
        pre_post_time = now + timedelta(minutes=post_time_delta_minutes)

    yesterday = now - timedelta(days=1)

    myprint(f"Getting post between : {yesterday} and {pre_post_time} (UTC)")

    try:
        with db_cursor() as cursor:
        # Get posts that have scheduled time between 24 hours ago and the pre_post_time
            # manual_publish rows are drafted for LinkedIn's native occasion composer, which has no
            # API entity (issue #1074) — the author publishes them by hand, so the scheduler must
            # never see one. Excluding them HERE makes that true for every consumer of this query.
            cursor.execute(
                """SELECT p.id, p.scheduled_time, p.user_id
                    FROM posts AS p
                    WHERE status = 'approved' AND manual_publish = 0
                      AND scheduled_time BETWEEN %s AND %s
                    ORDER BY scheduled_time ASC
                    """,
                (yesterday, pre_post_time,))
            posts = cursor.fetchall()
            # A non-empty poll is a real state transition worth keeping at INFO; an empty one is the
            # scheduler idling and was 220 identical rows in 48h of PostHog Logs.
            ready = [post[0] for post in posts]
            if ready:
                log_info(f"Posts ready to post: {ready}")
            else:
                log_debug("Posts ready to post: []")
    except mysql.connector.Error as err:
        log_error("Could not read the ready-to-post queue", exc=err,
                  task_name="auto_check_scheduled_posts")
        posts = []

    return posts


def get_orphaned_scheduled_posts(lookback_hours: int = 2) -> list:
    """Return posts stuck in 'scheduled' status that never reached 'posted'.

    These arise when Celery tasks are purged on container restart while a post
    has already been transitioned from 'approved' → 'scheduled'. Without this
    recovery query, those posts stay stuck forever.

    A `manual_publish` post is excluded for the same reason it is excluded upstream: only
    `auto_check_scheduled_posts` writes 'scheduled', and it never sees one — so a manual-publish row
    in that state is a bug, and re-queueing it would publish through the API the very post that
    exists because the API cannot carry it (issue #1074).
    """
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=lookback_hours)

    try:
        with db_cursor() as cursor:
            cursor.execute(
                """SELECT p.id, p.scheduled_time, p.user_id
                   FROM posts AS p
                   WHERE status = 'scheduled' AND manual_publish = 0
                     AND scheduled_time <= %s
                   ORDER BY scheduled_time ASC""",
                (cutoff,),
            )
            posts = cursor.fetchall()
            # Orphans found means the queue lost work — that stays at INFO. Finding none is the healthy
            # case and was 221 identical rows in 48h.
            orphaned = [p[0] for p in posts]
            if orphaned:
                log_info(f"Orphaned scheduled posts to re-queue: {orphaned}")
            else:
                log_debug("Orphaned scheduled posts to re-queue: []")
    except mysql.connector.Error as err:
        myprint(f"Could not get orphaned scheduled posts | Error: {err}")
        posts = []

    return posts


def get_user_password_pair_by_id(user_id: int):
    """The (email, decrypted password) pair the Selenium login uses.

    Always a two-tuple: a missing row or a failed read is `(None, None)`, never a bare None, so the unpack
    at every call site holds. A password that will not decrypt comes back None with the email intact.
    """
    try:
        with db_cursor(dictionary=True) as cursor:
            cursor.execute("SELECT email, password FROM users WHERE id = %s", (user_id,))

            user_password_pair = cursor.fetchone()
    except mysql.connector.Error as err:
        myprint(f"Could not get user password pair for user id: {user_id} | Error: {err}")
        user_password_pair = None

    if user_password_pair:
        return (user_password_pair['email'],
                decrypt_secret(user_password_pair['password'], user_id, SECRET_FIELD_PASSWORD))
    else:
        return None, None


def get_active_user_password_pairs():
    """`[email, password]` for every active user that has BOTH.

    A user missing either half is skipped silently — there is nothing a browser login could do with half
    a credential.
    """
    user_password_pairs = []

    active_users = get_active_user_ids()

    for user_id in active_users:
        email, password = get_user_password_pair_by_id(user_id)
        if email and password:
            user_password_pairs.append([email, password])

    return user_password_pairs


def add_linkedin_profile(profile: LinkedInProfile, user_id: Optional[int] = None):
    """Upsert a scraped LinkedIn profile.

    `user_id` is COALESCEd rather than overwritten, so re-scraping a profile with no account attached
    never unlinks a row that was already tied to one.
    """
    try:
        with db_cursor(commit=True) as cursor:
            cursor.execute("""
                INSERT INTO profiles (profile_url, email, data, user_id)
                VALUES (%s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE
                        profile_url = VALUES(profile_url),
                        email = VALUES(email),
                        data = VALUES(data),
                        user_id = COALESCE(VALUES(user_id), user_id)
                """,
                           (str(profile.profile_url), profile.email, profile.model_dump_json(), user_id))

            success = True
    except mysql.connector.Error as err:
        myprint(f"Could not add linkedin profile | Error: {err}")
        success = False
    return success


def get_linked_in_profile_by_url(profile_url: str, updated_less_than_days_ago: int = 1):
    """The stored profile JSON for a URL, as the one-column row tuple, but only while it is FRESH.

    `updated_less_than_days_ago` is a cache window, not a filter on the person: a row older than that
    reads as ABSENT so the caller re-scrapes instead of acting on stale headline/about text. Both slash
    spellings are queried because LinkedIn hands out both.
    """
    connection = _connection.get_db_connection()
    cursor = connection.cursor()

    profile_url_without_end_slash = profile_url.rstrip('/')
    profile_url_with_end_slash = profile_url_without_end_slash + '/'

    try:
        cursor.execute(
            "SELECT data FROM profiles WHERE (profile_url = %s or profile_url = %s) AND updated_at > NOW() - INTERVAL %s DAY",
            (profile_url_with_end_slash, profile_url_without_end_slash, updated_less_than_days_ago))
        profile_data = cursor.fetchone()
    except mysql.connector.Error as err:
        profile_data = None
        myprint(f"Could not get linkedin profile by url | Error: {err}")
    finally:
        cursor.close()
        connection.close()

    return profile_data


def get_linked_in_profile_by_email(profile_email: str, updated_less_than_days_ago: int = 1):
    """The stored profile JSON for an email, as the one-column row tuple.

    Same freshness window as `get_linked_in_profile_by_url` — a row older than
    `updated_less_than_days_ago` reads as absent rather than stale.
    """
    try:
        with db_cursor() as cursor:
            cursor.execute("SELECT data FROM profiles WHERE email = %s AND updated_at > NOW() - INTERVAL %s DAY",
                           (profile_email, updated_less_than_days_ago))
            profile_data = cursor.fetchone()
    except mysql.connector.Error as err:
        myprint(f"Could not get linkedin profile data by email | Error: {err}")
        profile_data = None

    return profile_data


def get_linked_in_profile_by_user_id(user_id: int, updated_less_than_days_ago: int = 1):
    """The stored profile JSON for a user, as the one-column row tuple.

    Same freshness window as `get_linked_in_profile_by_url` — a row older than
    `updated_less_than_days_ago` reads as absent rather than stale.
    """
    try:
        with db_cursor() as cursor:
            cursor.execute("SELECT data FROM profiles WHERE user_id = %s AND updated_at > NOW() - INTERVAL %s DAY",
                           (user_id, updated_less_than_days_ago))
            profile_data = cursor.fetchone()
    except mysql.connector.Error as err:
        myprint(f"Could not get linkedin profile data by user_id | Error: {err}")
        profile_data = None

    return profile_data


def get_profile_synthesis(user_id: int) -> Optional[tuple]:
    """Return the user's cached (synthesis_text, synthesis_generated_at) or None when there is no
    profile row / no synthesis yet. Kept separate from the profile-JSON getters so the small, stable
    voice brief can be read cheaply on every generation call without pulling the full profile blob.
    """
    try:
        with db_cursor() as cursor:
            cursor.execute(
                "SELECT synthesis, synthesis_generated_at FROM profiles WHERE user_id = %s", (user_id,))
            row = cursor.fetchone()
    except mysql.connector.Error as err:
        myprint(f"Could not get profile synthesis for user_id={user_id} | Error: {err}")
        row = None

    if not row or row[0] is None:
        return None
    return row[0], row[1]


def set_profile_synthesis(user_id: int, synthesis: str) -> bool:
    """Persist a freshly generated voice synthesis and stamp synthesis_generated_at = NOW() (drives
    the weekly staleness selector). No-op-safe: returns False if the profile row doesn't exist yet.
    """
    try:
        with db_cursor(commit=True) as cursor:
            cursor.execute(
                "UPDATE profiles SET synthesis = %s, synthesis_generated_at = NOW() WHERE user_id = %s",
                (synthesis, user_id))
            success = cursor.rowcount > 0
    except mysql.connector.Error as err:
        myprint(f"Could not set profile synthesis for user_id={user_id} | Error: {err}")
        success = False
    return success


def get_user_ids_needing_profile_synthesis(stale_days: int = 7) -> list:
    """User IDs whose cached profile synthesis is MISSING or older than `stale_days` — the work list
    for the weekly refresh task. Only rows that actually have a profile (user_id NOT NULL) qualify.
    """
    try:
        with db_cursor() as cursor:
            cursor.execute(
                "SELECT user_id FROM profiles WHERE user_id IS NOT NULL AND ("
                "synthesis IS NULL OR synthesis_generated_at IS NULL "
                "OR synthesis_generated_at < NOW() - INTERVAL %s DAY)",
                (stale_days,))
            rows = cursor.fetchall()
    except mysql.connector.Error as err:
        myprint(f"Could not get user_ids needing profile synthesis | Error: {err}")
        rows = []
    return [row[0] for row in rows]


def remove_linked_in_profile_by_user_id(user_id: int):
    """Drop this user's cached profile row so the next read re-scrapes.

    True means the DELETE ran, not that a row existed.
    """
    try:
        with db_cursor(commit=True) as cursor:
            cursor.execute("DELETE FROM profiles WHERE user_id = %s", (user_id,))
            success = True
    except mysql.connector.Error as err:
        myprint(f"Could not remove linkedin profile by user_id | Error: {err}")
        success = False
    return success


def remove_linked_in_profile_by_url(profile_url: str):
    """Drop the cached profile row for a URL so the next read re-scrapes.

    True means the DELETE ran, not that a row existed.
    """
    try:
        with db_cursor(commit=True) as cursor:
            cursor.execute("DELETE FROM profiles WHERE profile_url = %s", (profile_url,))
            success = True
    except mysql.connector.Error as err:
        myprint(f"Could not remove linkedin profile by url | Error: {err}")
        success = False
    return success


def remove_linked_in_profile_by_email(profile_email: str):
    """Drop the cached profile row for an email so the next read re-scrapes.

    True means the DELETE ran, not that a row existed.
    """
    try:
        with db_cursor(commit=True) as cursor:
            cursor.execute("DELETE FROM profiles WHERE email = %s", (profile_email,))
            success = True
    except mysql.connector.Error as err:
        myprint(f"Could not remove linkedin profile by email | Error: {err}")
        success = False
    return success


def get_post_type_counts(user_id: int):
    """Query the database to get the count of each post_type in the 'posts' table for the given user id."""
    try:
        with db_cursor(dictionary=True) as cursor:
            cursor.execute("SELECT post_type, COUNT(*) AS count FROM posts WHERE user_id = %s GROUP BY post_type",
                           (user_id,))
            post_counts = {row['post_type']: row['count'] for row in cursor.fetchall()}
    except mysql.connector.Error as err:
        myprint(f"Could not get post type counts | Error: {err}")
        post_counts = {}

    return post_counts


MAX_CONTENT_BUFFER_POSTS = 30
# A post counts against the buffer once its content exists: pending (awaiting approval), approved
# (queued) and scheduled (dispatched, not yet posted) are all "ready" and must not be re-generated.
READY_POST_STATUSES = ('pending', 'approved', 'scheduled')


def count_ready_posts_within_buffer(user_id: int, days: int = DEFAULT_CONTENT_BUFFER_DAYS) -> int:
    """Count posts that already have generated content due within the next `days` days."""
    try:
        with db_cursor() as cursor:
            cursor.execute(
                "SELECT COUNT(*) FROM posts"
                " WHERE user_id = %s"
                f" AND status IN ({', '.join(['%s'] * len(READY_POST_STATUSES))})"
                " AND scheduled_time BETWEEN NOW() AND NOW() + INTERVAL %s DAY",
                (user_id, *READY_POST_STATUSES, int(days)),
            )
            row = cursor.fetchone()
            return int(row[0]) if row else 0
    except mysql.connector.Error as err:
        myprint(f"Could not count ready posts within buffer for user_id {user_id} | Error: {err}")
        return 0


def get_planned_posts_within_buffer(user_id: int,
                                    days: int = DEFAULT_CONTENT_BUFFER_DAYS,
                                    max_posts: int = DEFAULT_CONTENT_BUFFER_MAX_POSTS,
                                    already_ready_count: int = 0) -> list[dict]:
    """Return the status=planning posts to generate now to top the buffer back up.

    Posts due within the next `days` days, soonest first, limited to
    `max_posts - already_ready_count` so we only fill the delta and never overshoot the cap.
    """
    limit = int(max_posts) - int(already_ready_count)
    if limit <= 0:
        return []

    try:
        with db_cursor(dictionary=True) as cursor:
            cursor.execute(
                # scheduled_time rides along so the generator can resolve the slot's day type
                # (issue #621) — the weekday IS the calendar key.
                "SELECT user_id, id, post_type, buyer_stage, content_mix, scheduled_time FROM posts"
                " WHERE status = 'planning' AND user_id = %s"
                " AND scheduled_time BETWEEN NOW() AND NOW() + INTERVAL %s DAY"
                " ORDER BY scheduled_time ASC, id ASC LIMIT %s",
                (user_id, int(days), limit),
            )
            planned_content = cursor.fetchall()
    except mysql.connector.Error as err:
        myprint(f"Could not get planned posts within buffer for user_id {user_id} | Error: {err}")
        planned_content = []

    return planned_content


def get_next_planned_posts_after_buffer(user_id: int, days: int, limit: int) -> list[dict]:
    """The soonest status=planning posts due BEYOND the buffer window, soonest first (issue #719).

    The pull-forward list for an explicitly requested run: when the window holds no planning rows
    (every near-term slot was posted or rejected, and rejected slots are never re-planned) the
    Generate button would otherwise no-op forever. Forward-only — a planning row already in the
    past is a stale slot, and generating content for a time that has passed would publish it
    immediately.
    """
    limit = int(limit)
    if limit <= 0:
        return []

    try:
        with db_cursor(dictionary=True) as cursor:
            cursor.execute(
                "SELECT user_id, id, post_type, buyer_stage, content_mix, scheduled_time FROM posts"
                " WHERE status = 'planning' AND user_id = %s"
                " AND scheduled_time > NOW() + INTERVAL %s DAY"
                " ORDER BY scheduled_time ASC, id ASC LIMIT %s",
                (user_id, int(days), limit),
            )
            planned_content = cursor.fetchall()
    except mysql.connector.Error as err:
        myprint(f"Could not get planned posts after buffer for user_id {user_id} | Error: {err}")
        planned_content = []

    return planned_content


def get_next_planned_post_date(user_id: int) -> Optional[datetime]:
    """When this user's soonest UPCOMING planning slot is due, or None when nothing is planned.

    Feeds the "nothing to generate right now" explanation (issue #719) — without a date the SPA
    can only say a run produced nothing, which reads as a broken feature.
    """
    try:
        with db_cursor() as cursor:
            cursor.execute(
                "SELECT MIN(scheduled_time) FROM posts"
                " WHERE status = 'planning' AND user_id = %s AND scheduled_time > NOW()",
                (user_id,),
            )
            row = cursor.fetchone()
            return row[0] if row else None
    except mysql.connector.Error as err:
        myprint(f"Could not get next planned post date for user_id {user_id} | Error: {err}")
        return None


def get_user_ids_with_planned_posts_within_buffer(days: int = MAX_CONTENT_BUFFER_DAYS) -> list[int]:
    """User IDs that have any status=planning post due within the next `days` days.

    Defaults to the max window so a user with a longer configured buffer is never missed by the
    beat's user discovery; the per-user window is applied by get_planned_posts_within_buffer.
    """
    try:
        with db_cursor() as cursor:
            cursor.execute(
                "SELECT DISTINCT user_id FROM posts"
                " WHERE status = 'planning'"
                " AND scheduled_time BETWEEN NOW() AND NOW() + INTERVAL %s DAY"
                " ORDER BY user_id",
                (int(days),),
            )
            return [row[0] for row in cursor.fetchall()]
    except mysql.connector.Error as err:
        myprint(f"Could not get user ids with planned posts within buffer | Error: {err}")
        return []


def get_last_planned_post_date_for_user(user_id: int):
    """Query the database to get the last planned post date for the given user."""
    try:
        with db_cursor() as cursor:
            cursor.execute(
                "SELECT MAX(scheduled_time) AS last_planned_date FROM posts "
                "WHERE user_id = %s AND status != 'rejected'",
                (user_id,))
            last_planned_date = cursor.fetchone()
    except mysql.connector.Error as err:
        myprint(f"Could not get last planned post date for user | Error: {err}")
        last_planned_date = None

    return last_planned_date[0] if last_planned_date else None


def get_user_blog_url(user_id: int):
    """Query the database to get the blog URL for the given user."""
    try:
        with db_cursor() as cursor:
            cursor.execute("SELECT blog_url FROM users WHERE id = %s", (user_id,))
            blog_url = cursor.fetchone()
    except mysql.connector.Error as err:
        myprint(f"Could not get user blog url | Error: {err}")
        blog_url = None

    return blog_url[0] if blog_url else None


def get_user_sitemap_url(user_id: int):
    """Query the database to get the sitemap URL for the given user."""
    try:
        with db_cursor() as cursor:
            cursor.execute("SELECT sitemap_url FROM users WHERE id = %s", (user_id,))
            sitemap_url = cursor.fetchone()
    except mysql.connector.Error as err:
        myprint(f"Could not get user sitemap url | Error: {err}")
        sitemap_url = None

    return sitemap_url[0] if sitemap_url else None


def get_linkedin_profile_url_by_user_id(user_id: int) -> Optional[str]:
    """Return the user's own LinkedIn profile URL (e.g. https://www.linkedin.com/in/<vanity>/).
    Only the user's own scraped profile carries a non-null user_id in the profiles table.
    """
    try:
        with db_cursor() as cursor:
            cursor.execute("SELECT profile_url FROM profiles WHERE user_id = %s LIMIT 1", (user_id,))
            row = cursor.fetchone()
    except mysql.connector.Error as err:
        myprint(f"Could not get user linkedin profile url | Error: {err}")
        row = None

    return row[0] if row else None


# `email` is deliberately NOT here (issue #950). An address is not a profile field: moving one has
# to write `user_email_history`, PIN the NEW address and revoke the account's other sessions, and
# `change_user_email` is the only path that does all three. `update_user` used to take an `email=`
# and UPDATE the column directly, which walked around every one of them — #914 removed its last
# caller, so what was left was a loaded footgun one keyword argument from being fired again.
_ALLOWED_USER_CLAUSES = frozenset({"blog_url = %s", "sitemap_url = %s"})


def update_user(user_id: int, blog_url: Optional[str] = None,
                sitemap_url: Optional[str] = None) -> bool:
    """Update the blog and/or sitemap URL on a user row; False when neither was supplied.

    Only fields that were passed become SET clauses, and each generated clause is re-checked against
    `_ALLOWED_USER_CLAUSES` before it is interpolated — see the note above that set for why `email` is
    deliberately not reachable from here.
    """
    if not any([blog_url, sitemap_url]):
        return False
    connection = _connection.get_db_connection()
    cursor = connection.cursor()
    fields, values = [], []
    if blog_url:
        fields.append("blog_url = %s")
        values.append(blog_url)
    if sitemap_url:
        fields.append("sitemap_url = %s")
        values.append(sitemap_url)
    for clause in fields:
        if clause not in _ALLOWED_USER_CLAUSES:
            raise ValueError(f"Disallowed SQL clause: {clause!r}")
    values.append(user_id)
    try:
        cursor.execute(f"UPDATE users SET {', '.join(fields)} WHERE id = %s", values)
        connection.commit()
        return cursor.rowcount > 0
    except mysql.connector.Error as err:
        myprint(f"Could not update user {user_id} | Error: {err}")
        return False
    finally:
        cursor.close()
        connection.close()


def get_active_user_ids():
    """Return user IDs eligible for automated posting/engagement.

    A user is active when ALL of:
      1. Has a valid LinkedIn connection (linkedin_connection_status = 'connected'
         AND access_token not expired)
      2. Has an active subscription OR an unexpired trial
      3. Has logged in within their configured inactivate delay
         (NULL delay = never auto-inactivate)
    """
    try:
        with db_cursor() as cursor:
            cursor.execute("""
                SELECT id FROM users
                WHERE
                    -- Must have a live LinkedIn token
                    linkedin_connection_status = 'connected'
                    AND access_token IS NOT NULL
                    AND access_token_created_at IS NOT NULL
                    AND access_token_created_at + INTERVAL access_token_expires_in SECOND > NOW()

                    -- Must have an active or unexpired trial subscription
                    AND (
                        subscription_status = 'active'
                        OR (
                            subscription_status = 'trial'
                            AND (trial_ends_at IS NULL OR trial_ends_at > NOW())
                        )
                    )

                    -- Must have logged in within their configured inactivity window.
                    -- NULL last_login (pre-session-migration users) is treated as active
                    -- so existing connected users are not silently dropped.
                    AND (
                        last_login_inactivate_delay IS NULL
                        OR last_login IS NULL
                        OR last_login >= NOW() - INTERVAL last_login_inactivate_delay DAY
                    )
            """)
            active_user_ids = [row[0] for row in cursor.fetchall()]
    except mysql.connector.Error as err:
        myprint(f"Could not get active user ids | Error: {err}")
        active_user_ids = []

    return active_user_ids


def get_linkedin_token_user_ids() -> list[int]:
    """Subscribed users holding a LinkedIn access token, expired or not (issue #600).

    Deliberately NOT get_active_user_ids(): that one requires an unexpired token, so the users the
    renewal pass most needs to reach — the ones whose authorization already lapsed — are exactly
    the ones it filters out.
    """
    try:
        with db_cursor() as cursor:
            cursor.execute("""
                SELECT id FROM users
                WHERE linkedin_connection_status = 'connected'
                  AND access_token IS NOT NULL
                  AND (
                        subscription_status = 'active'
                        OR (
                            subscription_status = 'trial'
                            AND (trial_ends_at IS NULL OR trial_ends_at > NOW())
                        )
                  )
            """)
            return [row[0] for row in cursor.fetchall()]
    except mysql.connector.Error as err:
        myprint(f"Could not get linkedin token user ids | Error: {err}")
        return []


def get_user_location(user_id: int) -> tuple[float, float] | None:
    """The user's Login Location as `(latitude, longitude)`, or None when it is not usable.

    A missing row, a failed read and a stored 0 all read as None: 0/0 is a point in the Atlantic, not a
    place anyone logs in from, so it must never reach the proxy/geo logic as a real coordinate.
    """
    try:
        with db_cursor() as cursor:
            cursor.execute("SELECT latitude, longitude FROM users WHERE id = %s", (user_id,))
            row = cursor.fetchone()
    except mysql.connector.Error as err:
        myprint(f"Could not get user location | Error: {err}")
        row = None
    return (float(row[0]), float(row[1])) if row and row[0] and row[1] else None






def has_user_commented_on_post_url(user_id: int, post_url: str):
    """Have we already left a top-level comment on this post URL?

    Replies do not count (see `count_user_comments_on_post_url`). A failed read counts zero, so an
    unreadable log reads as "not yet" and the post can be commented on again.
    """
    return count_user_comments_on_post_url(user_id, post_url) > 0












def get_post_status(post_id: int) -> str | None:
    """Return the current status string of a post, or None if not found."""
    try:
        with db_cursor() as cursor:
            cursor.execute("SELECT status FROM posts WHERE id = %s", (post_id,))
            row = cursor.fetchone()
    except mysql.connector.Error as err:
        myprint(f"Could not get post status | Error: {err}")
        row = None
    return row[0] if row else None


def get_company_linked_in_url_for_user(user_id: int):
    """The user's LinkedIn company page URL.

    None when it was never set or the read failed — the invite drip has no page to open in either case.
    """
    try:
        with db_cursor() as cursor:
            cursor.execute("SELECT company_linked_in_url FROM users WHERE id = %s", (user_id,))
            company_linked_in_url = cursor.fetchone()
    except mysql.connector.Error as err:
        myprint(f"Could not get user company linked in url | Error: {err}")
        company_linked_in_url = None

    return company_linked_in_url[0] if company_linked_in_url else None


def update_company_linked_in_url_for_user(user_id: int, company_linked_in_url: Optional[str]) -> bool:
    """Set (or clear, when None/empty) the user's LinkedIn company page URL used by the
    monthly company-page invite automation.
    """
    try:
        with db_cursor(commit=True) as cursor:
            cursor.execute(
                "UPDATE users SET company_linked_in_url = %s WHERE id = %s",
                (company_linked_in_url or None, user_id),
            )
            return cursor.rowcount >= 0
    except mysql.connector.Error as err:
        myprint(f"Could not update company linked in url for user {user_id} | Error: {err}")
        return False




def get_user_linkedin_display_name(user_id: int) -> Optional[str]:
    """The user's own name exactly as LinkedIn renders it on their messages (issue #731), or None.

    This is what reply detection compares the last sender against, so it is stored per user rather
    than re-derived from a scrape that may be stale or unavailable.
    """
    try:
        with db_cursor() as cursor:
            cursor.execute("SELECT linkedin_display_name FROM users WHERE id = %s", (user_id,))
            row = cursor.fetchone()
    except mysql.connector.Error as err:
        myprint(f"Could not get LinkedIn display name for user {user_id} | Error: {err}")
        return None
    name = (row[0] if row else None) or ""
    return name.strip() or None


def update_user_linkedin_display_name(user_id: int, display_name: Optional[str]) -> bool:
    """Set (or clear, when None/empty) the user's LinkedIn display name."""
    try:
        with db_cursor(commit=True) as cursor:
            cursor.execute(
                "UPDATE users SET linkedin_display_name = %s WHERE id = %s",
                ((display_name or "").strip() or None, user_id),
            )
            return cursor.rowcount >= 0
    except mysql.connector.Error as err:
        myprint(f"Could not update LinkedIn display name for user {user_id} | Error: {err}")
        return False


def update_user_linkedin_password(user_id: int, password: str) -> bool:
    """Store the user's LinkedIn login password for Selenium-driven automation.

    DEPRECATED (issue #745, design decision 2A): cookie-only (`li_at`) is the default now — see
    store_linkedin_li_at. The password must be stored reversibly because Selenium types it into
    the LinkedIn login form, so encryption at rest is the ceiling on how safe it can ever be;
    draining the column via clear_user_linkedin_password is the actual fix. Only call this from
    authenticated API endpoints — never expose the value in any response payload.
    """
    try:
        with db_cursor(commit=True) as cursor:
            cursor.execute(
                "UPDATE users SET password = %s WHERE id = %s",
                (encrypt_secret(password, user_id, SECRET_FIELD_PASSWORD), user_id),
            )
            return cursor.rowcount >= 0
    except mysql.connector.Error as err:
        myprint(f"Could not update LinkedIn password for user_id {user_id} | Error: {err}")
        return False


def clear_user_linkedin_password(user_id: int) -> bool:
    """Drop the stored LinkedIn password once the user has a session cookie instead (design §5.4).

    Encrypting the password still leaves a *decryptable* LinkedIn password in the DB, so the
    approved end state is to stop holding one at all. Called from the cookie-migration path, not
    on every cookie save — a user who has no li_at yet must keep their only working login.
    """
    try:
        with db_cursor(commit=True) as cursor:
            cursor.execute("UPDATE users SET password = NULL WHERE id = %s", (user_id,))
            log_info("Cleared stored LinkedIn password after cookie migration", user_id=user_id)
            return True
    except mysql.connector.Error as err:
        log_error(f"Could not clear LinkedIn password | Error: {err}", user_id=user_id)
        return False


def has_linkedin_password(user_id: int) -> bool:
    """True when a LinkedIn password is still stored for this user — the signal that drives the
    one-time 'paste a cookie instead' prompt (design §5.4 item 3).
    """
    try:
        with db_cursor() as cursor:
            cursor.execute(
                "SELECT 1 FROM users WHERE id = %s AND password IS NOT NULL AND password <> '' LIMIT 1",
                (user_id,),
            )
            return cursor.fetchone() is not None
    except mysql.connector.Error as err:
        log_error(f"Could not check stored LinkedIn password | Error: {err}", user_id=user_id)
        return False


def encrypt_secrets_at_rest(limit: Optional[int] = None) -> dict:
    """Backfill AND rotation in one pass (issue #745, design §5.1/§7 Stage 0).

    Rewrites every stored LinkedIn secret that is not already an envelope under the CURRENT key
    version: legacy plaintext gets encrypted, and a row still sealed under `LEM_SECRET_KEY_PREVIOUS`
    gets re-sealed under the new key. Both cases read through the same dual-mode `decrypt_secret`,
    which is why rotation needs no separate code path.

    **Idempotent** — a second run finds nothing to do, which is what makes it safe to schedule
    daily instead of hand-running once. A row that cannot be decrypted is counted and LEFT ALONE:
    overwriting it would destroy a secret that a corrected key might still recover.

    `plaintext_remaining` is the number the operator watches — once it reaches 0, `ENCRYPTION_REQUIRED`
    can be flipped and the legacy read path fails closed. It includes `orphaned` (see below), so the
    gate can never read 0 while a plaintext session is still sitting in a dump.
    """
    stats = {"enabled": encryption_enabled(), "scanned": 0, "rewritten": 0,
             "failed": 0, "orphaned": 0, "plaintext_remaining": 0}
    if not stats["enabled"]:
        log_warning("Secret encryption backfill skipped — no LEM_SECRET_KEY configured")
        return stats

    connection = _connection.get_db_connection()
    cursor = connection.cursor(dictionary=True)
    try:
        cursor.execute("SELECT id, password, access_token, refresh_token FROM users")
        user_rows = cursor.fetchall() or []
        cursor.execute("SELECT id, user_id, value FROM cookies WHERE user_id IS NOT NULL")
        cookie_rows = cursor.fetchall() or []
        # Rows with no user_id cannot be encrypted (nothing to bind the AAD to) and cannot be read
        # back (get_cookies JOINs users) — they are dead PLAINTEXT credentials. Counting them keeps
        # them out of the operator's blind spot: without this, the gate reports "0 unprotected"
        # while a legacy plaintext li_at is still in every dump. The remedy is deletion, not
        # encryption, so this pass reports them and never touches them.
        cursor.execute(
            "SELECT COUNT(*) AS n FROM cookies WHERE user_id IS NULL AND value IS NOT NULL "
            "AND value <> ''")
        orphan_row = cursor.fetchone() or {}
        stats["orphaned"] = int(orphan_row.get("n") or 0)
        stats["plaintext_remaining"] += stats["orphaned"]
    except mysql.connector.Error as err:
        log_error(f"Could not read secrets for encryption backfill | Error: {err}")
        cursor.close()
        connection.close()
        return stats

    # (UPDATE statement, row id, user id, field name, stored value). The statement is picked from
    # the fixed map above rather than composed from the row — nothing here is ever interpolated.
    targets: list[tuple[str, int, int, str, Optional[str]]] = []
    for row in user_rows:
        for column, field in (("password", SECRET_FIELD_PASSWORD),
                              ("access_token", SECRET_FIELD_ACCESS_TOKEN),
                              ("refresh_token", SECRET_FIELD_REFRESH_TOKEN)):
            targets.append((_SECRET_UPDATE_SQL[field], row["id"], row["id"], field, row[column]))
    for row in cookie_rows:
        targets.append((_SECRET_UPDATE_SQL[SECRET_FIELD_COOKIE_VALUE], row["id"], row["user_id"],
                        SECRET_FIELD_COOKIE_VALUE, row["value"]))

    try:
        for update_sql, row_id, user_id, field, value in targets:
            if not needs_reencrypt(value):
                continue
            stats["scanned"] += 1
            if limit is not None and stats["rewritten"] >= limit:
                stats["plaintext_remaining"] += 1
                continue
            plaintext = decrypt_secret(value, user_id, field)
            if not plaintext:
                stats["failed"] += 1
                stats["plaintext_remaining"] += 1
                continue
            try:
                cursor.execute(update_sql,
                               (encrypt_secret(plaintext, user_id, field), row_id))
                connection.commit()
                stats["rewritten"] += 1
            except mysql.connector.Error as err:
                log_error(f"Could not re-encrypt {field} for row {row_id} | Error: {err}",
                          user_id=user_id)
                stats["failed"] += 1
                stats["plaintext_remaining"] += 1
    finally:
        cursor.close()
        connection.close()

    if stats["orphaned"]:
        log_error(f"{stats['orphaned']} cookie row(s) have no user_id — plaintext sessions that "
                  f"cannot be encrypted or read. Delete them "
                  f"(DELETE FROM cookies WHERE user_id IS NULL) before flipping "
                  f"ENCRYPTION_REQUIRED.")
    log_info(f"Secret encryption backfill: {stats['rewritten']} rewritten, "
             f"{stats['failed']} failed, {stats['orphaned']} orphaned, "
             f"{stats['plaintext_remaining']} still unprotected")
    return stats


def update_user_settings(user_id: int, blog_url: str = None, sitemap_url: str = None) -> bool:
    """Write BOTH `blog_url` and `sitemap_url`, including as NULL when an argument is omitted.

    That is what separates it from `update_user`, which only touches the fields it was given: calling
    this with one URL CLEARS the other.
    """
    try:
        with db_cursor(commit=True) as cursor:
            cursor.execute(
                "UPDATE users SET blog_url = %s, sitemap_url = %s WHERE id = %s",
                (blog_url, sitemap_url, user_id)
            )
            success = cursor.rowcount >= 0
    except mysql.connector.Error as err:
        myprint(f"Could not update user settings | Error: {err}")
        success = False

    return success


# ---------------------------------------------------------------------------
# PIN authentication
# ---------------------------------------------------------------------------









# ---------------------------------------------------------------------------
# Session management
# ---------------------------------------------------------------------------

SESSION_SCOPE_EXTENSION = "extension"
SESSION_SCOPE_RECOVERY = "recovery"


def create_session(user_id: int, user_agent: Optional[str] = None,
                   ip: Optional[str] = None, label: Optional[str] = None,
                   scope: str = SESSION_SCOPE_FULL, verified: bool = False,
                   ttl_hours: Optional[int] = None) -> Optional[str]:
    """Mint a session and return the token to the CALLER ONLY — the row stores its SHA-256.

    Since #745 (2b) `sessions.session_token` holds the hash, so a DB dump hands over no live
    session. The device facts (user agent, pseudonymised IP, label) are what make per-device
    revocation possible on the account page.

    `verified=True` stamps the session as having proven a strong factor AT MINT TIME (2c) — a
    passkey login or PIN+TOTP. It is deliberately not the default: an email-PIN login and a
    recovery-code login both mint a session that cannot yet touch LinkedIn credentials.

    `ttl_hours` overrides the idle window for sessions that are NOT idle-driven (issue #1026). A
    headless agent runs on a schedule — a weekly one would find a 24h session dead every single
    run — so its row gets an explicit, longer life. It is still an ordinary revocable row on the
    Security card, so a long TTL is not a one-way door.
    """
    import secrets
    token = secrets.token_hex(32)
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(hours=ttl_hours if ttl_hours is not None else SESSION_IDLE_HOURS)
    try:
        with db_cursor(commit=True) as cursor:
            cursor.execute(
                "INSERT INTO sessions (session_token, user_id, expires_at, user_agent, ip_hash, "
                "last_seen_at, label, scope, last_verified_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (hash_session_token(token), user_id, expires_at, (user_agent or None),
                 hash_client_ip(ip), now, (label or _device_label(user_agent)), scope,
                 now if verified else None),
            )
            cursor.execute(
                "UPDATE users SET last_login = %s WHERE id = %s",
                (now, user_id),
            )
            return token
    except mysql.connector.Error as err:
        myprint(f"Could not create session for user_id {user_id} | Error: {err}")
        return None


def _device_label(user_agent: Optional[str]) -> str:
    """A short, human-readable name for a session row ("Chrome on macOS"). Best effort: the account
    page has to show the user something they can recognise before they revoke it.
    """
    if not user_agent:
        return "Unknown device"
    ua = user_agent.lower()
    browser = next((name for token, name in (
        ("edg/", "Edge"), ("opr/", "Opera"), ("chrome", "Chrome"), ("firefox", "Firefox"),
        ("safari", "Safari")) if token in ua), "Browser")
    platform = next((name for token, name in (
        ("iphone", "iPhone"), ("ipad", "iPad"), ("android", "Android"), ("mac os", "macOS"),
        ("macintosh", "macOS"), ("windows", "Windows"), ("linux", "Linux")) if token in ua),
        "unknown OS")
    return f"{browser} on {platform}"




def get_session_user_id(token: str) -> Optional[int]:
    """The user behind a live session token, or None. Thin wrapper over `resolve_session` so there
    stays exactly ONE place that validates a token and slides its expiry.
    """
    resolved = resolve_session(token)
    return resolved["user_id"] if resolved else None






def list_user_sessions(user_id: int, current_token: Optional[str] = None) -> list[dict]:
    """Live sessions for the account page. Never returns a token or a hash — the caller gets the
    row id it revokes by, plus enough device detail to recognise it. `is_current` is resolved here
    so the SPA never has to compare tokens.
    """
    current_hash = hash_session_token(current_token)
    try:
        with db_cursor(dictionary=True) as cursor:
            cursor.execute(
                "SELECT id, session_token, label, user_agent, created_at, last_seen_at, expires_at "
                "FROM sessions WHERE user_id = %s AND revoked_at IS NULL AND expires_at > %s "
                "ORDER BY COALESCE(last_seen_at, created_at) DESC",
                (user_id, datetime.now(timezone.utc)),
            )
            sessions = []
            for row in cursor.fetchall():
                sessions.append({
                    "id": row["id"],
                    "label": row.get("label") or _device_label(row.get("user_agent")),
                    "created_at": row.get("created_at"),
                    "last_seen_at": row.get("last_seen_at"),
                    "expires_at": row.get("expires_at"),
                    "is_current": bool(current_hash) and row.get("session_token") == current_hash,
                })
            return sessions
    except mysql.connector.Error as err:
        myprint(f"Could not list sessions for user_id {user_id} | Error: {err}")
        return []






# ---------------------------------------------------------------------------
# Auth audit log (issue #745, 2b)
# ---------------------------------------------------------------------------







# ---------------------------------------------------------------------------
# Strong authentication factors, recovery codes and ceremony state (issue #745, 2c)
#
# Every SQL statement 2c needs lives here; the policy that reads it is
# `utilities/auth_factors.py`, and the ceremony wrappers are `utilities/webauthn_util.py`.
# ---------------------------------------------------------------------------




def _credential_id_hash(credential_id: Optional[str]) -> Optional[str]:
    """SHA-256 of a base64url credential id — what carries the UNIQUE index and every lookup.

    A credential id is public (the browser hands it to any site that asks), so this is a length
    normaliser, not a secret-protection measure: raw ids run past what MySQL will index.
    """
    if not credential_id:
        return None
    return hashlib.sha256(credential_id.encode("utf-8")).hexdigest()


def add_passkey_factor(user_id: int, credential_id: str, public_key: str, sign_count: int = 0,
                       label: Optional[str] = None, transports: Optional[str] = None) -> Optional[int]:
    """Store a verified passkey. Confirmed on insert — a registration response only reaches here
    after `verify_registration_response` accepted it, so there is no unproven state to hold.
    """
    try:
        with db_cursor(commit=True) as cursor:
            now = datetime.now(timezone.utc)
            cursor.execute(
                """INSERT INTO user_auth_factors
                   (user_id, kind, label, credential_id, credential_id_hash, public_key, sign_count,
                    transports, confirmed_at)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                (user_id, AUTH_FACTOR_PASSKEY, (label or "Passkey")[:120], credential_id,
                 _credential_id_hash(credential_id), public_key, int(sign_count),
                 (transports or None), now),
            )
            return cursor.lastrowid
    except mysql.connector.Error as err:
        if err.errno == errorcode.ER_DUP_ENTRY:
            log_warning("Passkey already registered", user_id=user_id)
            return None
        myprint(f"Could not store passkey for user_id {user_id} | Error: {err}")
        return None


def get_passkey_by_credential_id(credential_id: str) -> Optional[dict]:
    """The stored passkey for a credential id, with the user it belongs to. This is how a
    discoverable-credential login resolves WHO is signing in — the assertion names the credential,
    not the account.
    """
    try:
        with db_cursor(dictionary=True) as cursor:
            cursor.execute(
                """SELECT id, user_id, credential_id, public_key, sign_count, label
                   FROM user_auth_factors
                   WHERE credential_id_hash = %s AND kind = %s AND confirmed_at IS NOT NULL""",
                (_credential_id_hash(credential_id), AUTH_FACTOR_PASSKEY),
            )
            return cursor.fetchone()
    except mysql.connector.Error as err:
        myprint(f"Could not look up passkey | Error: {err}")
        return None
















































# ---------------------------------------------------------------------------
# User helpers
# ---------------------------------------------------------------------------

def add_user_by_email(email: str) -> Optional[int]:
    """Create a trial account for an email and return its id, minting the `public_uid` identity up front.

    The trial window is stamped here from `FREE_TRIAL_DAYS`, so the clock starts at signup rather than at
    first login. Stripe customer creation is best-effort — a Stripe outage must not cost us the account
    row — and a duplicate email returns the EXISTING user's id, which makes a replayed signup idempotent
    instead of an error.
    """
    from cqc_lem.utilities.env_constants import FREE_TRIAL_DAYS
    now = datetime.now(timezone.utc)
    trial_ends = now + timedelta(days=FREE_TRIAL_DAYS)
    connection = _connection.get_db_connection()
    cursor = connection.cursor()
    try:
        cursor.execute(
            """INSERT INTO users
               (email, public_uid, subscription_status, subscription_tier, trial_started_at,
                trial_ends_at)
               VALUES (%s, %s, 'trial', 'free_trial', %s, %s)""",
            (email, str(uuid.uuid4()), now, trial_ends),
        )
        connection.commit()
        user_id = cursor.lastrowid
        # Create a Stripe customer in the background (non-fatal if it fails)
        try:
            from cqc_lem.utilities.stripe_util import create_stripe_customer
            stripe_cid = create_stripe_customer(email, user_id)
            if stripe_cid:
                cursor.execute(
                    "UPDATE users SET stripe_customer_id = %s WHERE id = %s",
                    (stripe_cid, user_id),
                )
                connection.commit()
        except Exception as se:
            myprint(f"Stripe customer creation non-fatal error for {email}: {se}")
        return user_id
    except mysql.connector.Error as err:
        if err.errno == errorcode.ER_DUP_ENTRY:
            return get_user_id(email)
        myprint(f"Could not create user for {email} | Error: {err}")
        return None
    finally:
        cursor.close()
        connection.close()


def get_user_public_uid(user_id: int) -> Optional[str]:
    """The account's public identifier (issue #745, 2b). Lazily minted for a row that predates the
    column and somehow escaped the migration backfill, so callers never have to handle None.
    """
    connection = _connection.get_db_connection()
    cursor = connection.cursor(dictionary=True)
    try:
        cursor.execute("SELECT public_uid FROM users WHERE id = %s", (user_id,))
        row = cursor.fetchone()
        if not row:
            return None
        if row.get('public_uid'):
            return row['public_uid']
        public_uid = str(uuid.uuid4())
        cursor.execute("UPDATE users SET public_uid = %s WHERE id = %s AND public_uid IS NULL",
                       (public_uid, user_id))
        connection.commit()
        return public_uid
    except mysql.connector.Error as err:
        myprint(f"Could not get public_uid for user_id {user_id} | Error: {err}")
        return None
    finally:
        cursor.close()
        connection.close()


def get_user_id_by_public_uid(public_uid: str) -> Optional[int]:
    """Resolve the public identity (`users.public_uid`) back to the internal row id.

    None when it matches nothing or the read failed.
    """
    try:
        with db_cursor(dictionary=True) as cursor:
            cursor.execute("SELECT id FROM users WHERE public_uid = %s", (public_uid,))
            row = cursor.fetchone()
            return row['id'] if row else None
    except mysql.connector.Error as err:
        myprint(f"Could not resolve public_uid | Error: {err}")
        return None


def mark_email_verified(user_id: int) -> bool:
    """Stamp `users.email_verified_at` — the email is an attribute of the account, and this is the
    proof that the current value was actually reached.
    """
    try:
        with db_cursor(commit=True) as cursor:
            cursor.execute("UPDATE users SET email_verified_at = %s WHERE id = %s",
                           (datetime.now(timezone.utc), user_id))
            return cursor.rowcount >= 0
    except mysql.connector.Error as err:
        myprint(f"Could not mark email verified for user_id {user_id} | Error: {err}")
        return False


def change_user_email(user_id: int, new_email: str,
                      changed_by_session_id: Optional[int] = None) -> bool:
    """Point the account at a different email and record the move in `user_email_history`.

    The account identity is `users.id` / `public_uid`, so nothing else has to move. Returns False
    when the new address already belongs to another account — the caller must not merge accounts.
    """
    connection = _connection.get_db_connection()
    cursor = connection.cursor(dictionary=True)
    try:
        now = datetime.now(timezone.utc)
        cursor.execute("SELECT id FROM users WHERE email = %s AND id <> %s", (new_email, user_id))
        if cursor.fetchone():
            log_warning("Email change rejected — address already in use", user_id=user_id)
            return False

        cursor.execute("SELECT email FROM users WHERE id = %s", (user_id,))
        row = cursor.fetchone()
        if not row:
            return False
        old_email = row.get('email')

        cursor.execute(
            "UPDATE users SET email = %s, email_verified_at = %s WHERE id = %s",
            (new_email, now, user_id),
        )
        cursor.execute(
            "INSERT INTO user_email_history (user_id, old_email, new_email, changed_by_session_id) "
            "VALUES (%s, %s, %s, %s)",
            (user_id, old_email, new_email, changed_by_session_id),
        )
        connection.commit()
        return True
    except mysql.connector.Error as err:
        if err.errno == errorcode.ER_DUP_ENTRY:
            log_warning("Email change rejected — address already in use", user_id=user_id)
            return False
        myprint(f"Could not change email for user_id {user_id} | Error: {err}")
        return False
    finally:
        cursor.close()
        connection.close()


def get_user_email(user_id: int) -> Optional[str]:
    """The account's current email address — an attribute of the account, not its identity (issue #745)."""
    try:
        with db_cursor(dictionary=True) as cursor:
            cursor.execute("SELECT email FROM users WHERE id = %s", (user_id,))
            row = cursor.fetchone()
            return row['email'] if row else None
    except mysql.connector.Error as err:
        myprint(f"Could not get email for user_id {user_id} | Error: {err}")
        return None


def get_user_analytics_profile(user_id: int) -> dict:
    """The non-sensitive person facts the SPA sets on the PostHog person at $identify (issue #646):
    plan tier/status, timezone and the signup timestamp. `users` has no created_at column, so the
    signup time is trial_started_at falling back to updated_at — the same convention the cohort
    query uses. Never returns credentials; the SPA already knows the email.

    Issue #653 adds the two facts PostHog Surveys TARGET on: when onboarding actually completed (the
    activation "aha", not signup — a user who signed up and stalled has no opinion worth surveying)
    and how many posts the user has ever approved. "Ever approved" is deliberately not
    `status='approved'`: an approved post moves on to scheduled and then posted, so counting the
    current status alone would reset the tally the moment automation ran.
    """
    try:
        with db_cursor(dictionary=True) as cursor:
            cursor.execute(
                "SELECT u.subscription_tier, u.subscription_status, u.timezone, "
                "COALESCE(u.trial_started_at, u.updated_at) AS created_at, "
                "o.activated_at AS onboarding_completed_at, "
                "(SELECT COUNT(*) FROM posts p WHERE p.user_id = u.id "
                " AND p.status IN (%s, %s, %s)) AS posts_approved "
                "FROM users u LEFT JOIN onboarding_state o ON o.user_id = u.id "
                "WHERE u.id = %s",
                (str(PostStatus.APPROVED), str(PostStatus.SCHEDULED), str(PostStatus.POSTED), user_id))
            row = cursor.fetchone()
            return row or {}
    except mysql.connector.Error as err:
        myprint(f"Could not get analytics profile for user_id {user_id} | Error: {err}")
        return {}


def get_user_token_info(user_id: int) -> Optional[dict]:
    """The LinkedIn OAuth token row with both tokens decrypted, or None.

    Deliberately does NOT filter on expiry the way `get_user_access_token` does: this is the input to the
    expiry decision (`resolve_token_status`), so an expired token has to come back for the SPA countdown
    and the renewal beat to be able to see it at all.
    """
    try:
        with db_cursor(dictionary=True) as cursor:
            cursor.execute(
                """SELECT access_token, access_token_expires_in, access_token_created_at,
                          refresh_token, refresh_token_expires_in, refresh_token_created_at
                   FROM users WHERE id = %s""",
                (user_id,),
            )
            row = cursor.fetchone()
            if row:
                row['access_token'] = decrypt_secret(
                    row.get('access_token'), user_id, SECRET_FIELD_ACCESS_TOKEN)
                row['refresh_token'] = decrypt_secret(
                    row.get('refresh_token'), user_id, SECRET_FIELD_REFRESH_TOKEN)
            return row
    except mysql.connector.Error as err:
        myprint(f"Could not get token info for user_id {user_id} | Error: {err}")
        return None


def update_user_access_token(
    user_id: int,
    access_token: str,
    expires_in: int,
    refresh_token: Optional[str] = None,
    refresh_token_expires_in: Optional[int] = None,
) -> bool:
    """Store a refreshed LinkedIn access token, sealed, and restamp its created_at.

    The refresh token is only written when one was supplied: LinkedIn does not always return a new one,
    and blanking the stored one would end the renewal chain that is the only way auth outlives LinkedIn's
    60-day cap. False when no row matched.
    """
    now = datetime.now(timezone.utc)
    try:
        with db_cursor(commit=True) as cursor:
            if refresh_token:
                cursor.execute(
                    """UPDATE users SET
                           access_token = %s,
                           access_token_expires_in = %s,
                           access_token_created_at = %s,
                           refresh_token = %s,
                           refresh_token_expires_in = %s,
                           refresh_token_created_at = %s
                       WHERE id = %s""",
                    (encrypt_secret(access_token, user_id, SECRET_FIELD_ACCESS_TOKEN),
                     expires_in, now,
                     encrypt_secret(refresh_token, user_id, SECRET_FIELD_REFRESH_TOKEN),
                     refresh_token_expires_in, now, user_id),
                )
            else:
                cursor.execute(
                    """UPDATE users SET
                           access_token = %s,
                           access_token_expires_in = %s,
                           access_token_created_at = %s
                       WHERE id = %s""",
                    (encrypt_secret(access_token, user_id, SECRET_FIELD_ACCESS_TOKEN),
                     expires_in, now, user_id),
                )
            return cursor.rowcount > 0
    except mysql.connector.Error as err:
        myprint(f"Could not update access token for user_id {user_id} | Error: {err}")
        return False


def update_user_linkedin_token(
    user_id: int,
    linked_sub_id: str,
    access_token: str,
    expires_in: int,
    refresh_token: Optional[str] = None,
    refresh_token_expires_in: Optional[int] = None,
    linkedin_email: Optional[str] = None,
) -> bool:
    """Write a fresh LinkedIn OAuth token to the user identified by user_id.

    Called from the OAuth callback so the token is always attached to the
    logged-in user, regardless of which email LinkedIn returns.
    """
    now = datetime.now(timezone.utc)
    try:
        with db_cursor(commit=True) as cursor:
            if refresh_token:
                cursor.execute(
                    """UPDATE users SET
                           linked_sub_id = %s,
                           linkedin_email = %s,
                           access_token = %s,
                           access_token_expires_in = %s,
                           access_token_created_at = %s,
                           refresh_token = %s,
                           refresh_token_expires_in = %s,
                           refresh_token_created_at = %s,
                           linkedin_connection_status = 'connected'
                       WHERE id = %s""",
                    (linked_sub_id, linkedin_email or None,
                     encrypt_secret(access_token, user_id, SECRET_FIELD_ACCESS_TOKEN),
                     expires_in, now,
                     encrypt_secret(refresh_token, user_id, SECRET_FIELD_REFRESH_TOKEN),
                     refresh_token_expires_in, now, user_id),
                )
            else:
                cursor.execute(
                    """UPDATE users SET
                           linked_sub_id = %s,
                           linkedin_email = %s,
                           access_token = %s,
                           access_token_expires_in = %s,
                           access_token_created_at = %s,
                           linkedin_connection_status = 'connected'
                       WHERE id = %s""",
                    (linked_sub_id, linkedin_email or None,
                     encrypt_secret(access_token, user_id, SECRET_FIELD_ACCESS_TOKEN),
                     expires_in, now, user_id),
                )
            return cursor.rowcount > 0
    except mysql.connector.Error as err:
        myprint(f"Could not update LinkedIn token for user_id {user_id} | Error: {err}")
        return False


def update_linkedin_connection_status(user_id: int, status: str) -> bool:
    """Set linkedin_connection_status to 'connected', 'expired', or 'disconnected'."""
    try:
        with db_cursor(commit=True) as cursor:
            cursor.execute(
                "UPDATE users SET linkedin_connection_status = %s WHERE id = %s",
                (status, user_id),
            )
            return cursor.rowcount > 0
    except mysql.connector.Error as err:
        myprint(f"Could not update linkedin_connection_status for user_id {user_id} | Error: {err}")
        return False


def get_user_subscription_info(user_id: int) -> Optional[dict]:
    """Return subscription fields for the given user."""
    try:
        with db_cursor(dictionary=True) as cursor:
            cursor.execute(
                """SELECT subscription_status, subscription_tier,
                          trial_started_at, trial_ends_at,
                          stripe_customer_id, stripe_subscription_id
                   FROM users WHERE id = %s""",
                (user_id,),
            )
            return cursor.fetchone()
    except mysql.connector.Error as err:
        myprint(f"Could not get subscription info for user_id {user_id} | Error: {err}")
        return None


def update_subscription_from_stripe(
    stripe_customer_id: str,
    status: str,
    tier: Optional[str],
    subscription_id: Optional[str],
    current_period_end: Optional[datetime] = None,
) -> bool:
    """Called from Stripe webhook handler to sync subscription state.

    When tier is None (e.g. subscription deleted) we preserve the existing tier so
    historical data is retained. Pass an explicit empty string to clear it.
    """
    try:
        with db_cursor(commit=True) as cursor:
            if tier is not None:
                cursor.execute(
                    """UPDATE users
                       SET subscription_status = %s,
                           subscription_tier = %s,
                           stripe_subscription_id = %s,
                           subscription_current_period_end = %s
                       WHERE stripe_customer_id = %s""",
                    (status, tier, subscription_id, current_period_end, stripe_customer_id),
                )
            else:
                # Don't overwrite the tier — preserve it for historical reference
                cursor.execute(
                    """UPDATE users
                       SET subscription_status = %s,
                           stripe_subscription_id = %s,
                           subscription_current_period_end = %s
                       WHERE stripe_customer_id = %s""",
                    (status, subscription_id, current_period_end, stripe_customer_id),
                )
            return cursor.rowcount > 0
    except mysql.connector.Error as err:
        myprint(f"Could not update subscription from Stripe for customer {stripe_customer_id} | Error: {err}")
        return False


def get_users_with_stripe_subscriptions() -> list[dict]:
    """Return all users that have a Stripe subscription ID (for periodic sync)."""
    try:
        with db_cursor(dictionary=True) as cursor:
            cursor.execute(
                """SELECT id, stripe_customer_id, stripe_subscription_id,
                          subscription_status, subscription_tier
                   FROM users
                   WHERE stripe_subscription_id IS NOT NULL
                     AND subscription_status IN ('active', 'past_due')"""
            )
            return cursor.fetchall() or []
    except mysql.connector.Error as err:
        myprint(f"Could not fetch Stripe subscribers | Error: {err}")
        return []


def get_user_preferences(user_id: int) -> dict:
    """Return user preference fields with safe defaults.

    Defaults auto_schedule_posts=True so new users' content is automatically
    queued without requiring manual opt-in.
    """
    _defaults: dict = {"last_login_inactivate_delay": None, "auto_schedule_posts": True,
                       "content_buffer_days": DEFAULT_CONTENT_BUFFER_DAYS,
                       "content_buffer_max_posts": DEFAULT_CONTENT_BUFFER_MAX_POSTS,
                       "content_language": None}
    try:
        with db_cursor(dictionary=True) as cursor:
            cursor.execute(
                "SELECT last_login_inactivate_delay, auto_schedule_posts,"
                " content_buffer_days, content_buffer_max_posts, content_language FROM users WHERE id = %s",
                (user_id,),
            )
            row = cursor.fetchone()
            return row if row is not None else _defaults
    except mysql.connector.Error as err:
        myprint(f"Could not get preferences for user_id {user_id} | Error: {err}")
        return _defaults


def update_user_preferences(
    user_id: int,
    inactivate_delay: Optional[int],
    auto_schedule_posts: bool,
    content_buffer_days: Optional[int] = None,
    content_buffer_max_posts: Optional[int] = None,
    content_language: Optional[str] = None,
) -> bool:
    """Persist user-configurable inactivity delay (None = never) and auto-schedule flag.

    The content-buffer knobs and the content language are left untouched when None so a client
    that doesn't send them (the current Account UI) never resets them. An empty-string
    content_language DOES clear it, returning the user to the Login Location default.
    """
    sets = ["last_login_inactivate_delay = %s", "auto_schedule_posts = %s"]
    params: list = [inactivate_delay, 1 if auto_schedule_posts else 0]
    if content_buffer_days is not None:
        sets.append("content_buffer_days = %s")
        params.append(max(1, min(MAX_CONTENT_BUFFER_DAYS, int(content_buffer_days))))
    if content_buffer_max_posts is not None:
        sets.append("content_buffer_max_posts = %s")
        params.append(max(1, min(MAX_CONTENT_BUFFER_POSTS, int(content_buffer_max_posts))))
    if content_language is not None:
        sets.append("content_language = %s")
        params.append(content_language.strip()[:16] or None)
    params.append(user_id)

    try:
        with db_cursor(commit=True) as cursor:
            cursor.execute(
                f"UPDATE users SET {', '.join(sets)} WHERE id = %s",
                tuple(params),
            )
            # rowcount==0 means the row existed but values were unchanged — still a success
            return cursor.rowcount >= 0
    except mysql.connector.Error as err:
        myprint(f"Could not update preferences for user_id {user_id} | Error: {err}")
        return False


# Catch-up milestone types eligible for a congratulations touch out of the box (issue #482): the two
# real trigger events. All six types are user-configurable; birthdays/anniversaries are opt-in because
# congratulating those at volume reads as spam.
DEFAULT_CATCHUP_EVENT_TYPES = ("job_change", "promotion")
VALID_CATCHUP_TOUCH_MODES = ("pre_review", "auto_approve")
# Where the congratulations text comes from. 'linkedin' = LinkedIn's own pre-drafted response for the
# moment (no LLM); 'ai' = the DM-template + voice-refinement path, for users who want more customization.
VALID_CATCHUP_MESSAGE_SOURCES = ("linkedin", "ai")
# Per-day cap bounds. 5/day is the ceiling on every plan; raising it to 10/day is a premium feature
# (owner review on PR #509: "3A, but use 3B as a premium subscribed user feature").
CATCHUP_TOUCHES_MIN = 0
CATCHUP_TOUCHES_MAX_STANDARD = 5
CATCHUP_TOUCHES_MAX_PREMIUM = 10
# Absolute ceiling accepted at the API boundary — the per-user allowance is applied on top of it.
CATCHUP_TOUCHES_MAX = CATCHUP_TOUCHES_MAX_PREMIUM
# Per-contact cooldown across ALL catch-up event types (issue #1078). A new congratulations to the
# same person is held until at least this many days have passed since the last one.
CATCHUP_MIN_CONTACT_INTERVAL_DAYS_DEFAULT = 7
CATCHUP_MIN_CONTACT_INTERVAL_DAYS_MIN = 0
CATCHUP_MIN_CONTACT_INTERVAL_DAYS_MAX = 365
# Per-contact rolling cap (issue #1078). At most this many catch-up messages may reach the same
# person within CATCHUP_CONTACT_CAP_WINDOW_DAYS. 0 means no cap.
#
# The cap window is deliberately NOT the cooldown window: the cooldown already blocks every send
# inside its own window, so a cap measured over the same span could never be reached (the first
# message would trip the cooldown long before the second reached the cap), and disabling the
# cooldown would silently disable the cap too. A month-long window makes the cap the second,
# independent bound the reporter asked for — "no more than N catch-ups to this person, ever, in a
# rolling month" — regardless of how the cooldown is set.
CATCHUP_MAX_PER_CONTACT_DAYS_DEFAULT = 2
CATCHUP_MAX_PER_CONTACT_DAYS_MIN = 0
CATCHUP_MAX_PER_CONTACT_DAYS_MAX = 365
# The rolling window the per-contact cap is measured over. Fixed, not a preference: the cap and the
# cooldown are two different questions, and one knob answering both is how the cap became unreachable.
CATCHUP_CONTACT_CAP_WINDOW_DAYS = 30
# Paid plans that unlock the premium catch-up allowance (see stripe_util.TIER_PRICE_MAP).
PREMIUM_SUBSCRIPTION_TIERS = ("professional", "enterprise")
ACTIVE_SUBSCRIPTION_STATUSES = ("active", "trial")


def is_premium_subscriber(user_id: int) -> bool:
    """True when the user is on a currently-active professional/enterprise plan. Anything else —
    free trial, starter, lapsed, unknown, or a DB error — is treated as NOT premium, so a premium-only
    allowance can never be granted by accident.
    """
    try:
        info = get_user_subscription_info(user_id)
    except Exception:
        return False
    if not info:
        return False
    return (str(info.get("subscription_tier") or "") in PREMIUM_SUBSCRIPTION_TIERS
            and str(info.get("subscription_status") or "") in ACTIVE_SUBSCRIPTION_STATUSES)


def max_catchup_touches_allowed(user_id: int) -> int:
    """The highest catch-up cap this user may set — 10/day on premium plans, 5/day otherwise."""
    return CATCHUP_TOUCHES_MAX_PREMIUM if is_premium_subscriber(user_id) else CATCHUP_TOUCHES_MAX_STANDARD


# Publishing cadence (issue #621 / G6). 2-4 high-effort posts a week beat daily volume in the 2026
# regime — van der Blom's 1.3M-post sample puts daily posting at roughly -26% average reach per
# post — so the default drops from one-a-day to 3/week. 7 (daily) stays reachable for users who
# insist on it, which is why the ceiling is a full week rather than 5; the SPA warns above 4.
POSTS_PER_WEEK_MIN, POSTS_PER_WEEK_MAX = 2, 7
DEFAULT_POSTS_PER_WEEK = 3

# WHICH weekdays those slots may land on (issue #581). Mon=0 … Sun=6, default Mon-Fri: weekends are
# opt-in rather than the automatic consequence of raising the cadence to 6-7/week. All seven days
# stay selectable — this is an allow-list, never a hardcoded work week. `posts_per_week` still
# decides how many of the allowed days are actually filled.
DEFAULT_POSTING_DAYS = [0, 1, 2, 3, 4]
POSTING_DAY_MIN, POSTING_DAY_MAX = 0, 6


def normalize_posting_days(value) -> list:
    """A de-duped, sorted list of valid weekday ints — or the Mon-Fri default when the input holds
    nothing usable. Never returns an empty set: an empty cadence would schedule no content at all,
    and a bad value must not be persisted into the one-row prefs upsert (the V52 lesson).
    """
    days = []
    for raw in _coerce_json_list(value):
        try:
            day = int(raw)
        except (TypeError, ValueError):
            continue
        if POSTING_DAY_MIN <= day <= POSTING_DAY_MAX and day not in days:
            days.append(day)
    return sorted(days) if days else list(DEFAULT_POSTING_DAYS)


# Company-page invites per day (issue #732). LinkedIn Pages spend a MONTHLY credit pool that renews
# on the 1st and is refunded when an invite is accepted; LinkedIn is currently cutting the free-Page
# allowance from 250 to 50/month, so a drip has to survive both sizes. 5/day is the conservative
# ceiling: at 50 credits the credits/days-left spread binds first (~2/day), at 250 this binds, and
# the #626 budget draw (40-100% of cap) keeps the realised average lower still. 0 turns the lane off.
COMPANY_PAGE_INVITES_PER_DAY_DEFAULT = 5
COMPANY_PAGE_INVITES_PER_DAY_MIN, COMPANY_PAGE_INVITES_PER_DAY_MAX = 0, 50

# Roster auto-follows per day (issue #962). Far smaller than any other lane on purpose: a follow is
# the cheapest action to automate and the easiest to over-run, and LinkedIn's own follow limits are
# what a bulk-follower trips first. 3/day is a catch-up rate — a 50-account roster reaches full
# coverage in a few weeks — and the #626 budget draw (40-100% of cap, plus rest days) keeps the
# realised average below it. 0 turns the lane off without touching the toggle.
ROSTER_FOLLOWS_PER_DAY_DEFAULT = 3
ROSTER_FOLLOWS_PER_DAY_MIN, ROSTER_FOLLOWS_PER_DAY_MAX = 0, 20

_ENGAGEMENT_DEFAULTS: dict = {
    # Default to MEDIUM (issue #394): 2026 LinkedIn weights substantive ≥15-word comments ~2.5× short
    # one-liners, so the out-of-the-box length produces a real, specific reply rather than a throwaway.
    "tone": None, "comment_length": "medium", "comment_style": None,
    # use_hashtags stays OFF by default (issue #393): hashtags no longer expand reach in 2026 and
    # hashtag-free posts out-perform tagged ones. See content_framework.hashtag_directive.
    "use_emojis": True, "use_hashtags": False,
    "include_topics": [], "exclude_topics": [], "include_keywords": [], "exclude_keywords": [],
    "include_authors": [], "exclude_authors": [], "post_types": [],
    "focus_topics": [], "business_goals": None, "personal_goals": None,
    # Quality-gate thresholds (issue #421). None = follow the deploy default
    # (AUTHENTICITY_SCORE_MIN / POST_SIMILARITY_MAX), so the gates behave exactly as before until
    # the user tunes them.
    "authenticity_score_min": None, "post_similarity_max_pct": None,
    "min_reactions": None, "max_post_age_hours": 24, "reply_to_own_comments": True,
    "max_comments_per_day": 20, "max_dms_per_day": 20, "max_invites_per_day": 10,
    # Company-page invites (issue #732) run on their OWN small cap, and the effective ceiling is
    # min(this, max_invites_per_day) — see COMPANY_PAGE_INVITES_PER_DAY_DEFAULT for why 5.
    "max_company_page_invites_per_day": COMPANY_PAGE_INVITES_PER_DAY_DEFAULT,
    "connection_request_mode": "auto_approve",
    # Smart connection targeting (issue #486). 'suggest' sources candidates but always files them as
    # drafts, so enabling targeting can never send outbound on its own.
    "connection_targeting_mode": "suggest", "connection_target_authors": [],
    "min_connection_icp_score": 55,
    "default_buyer_stage": None,
    "default_video_quality": "standard",
    "reply_check_mode": "event", "reply_sweeps_per_day": 2, "reply_max_post_age_days": 2,
    # feed_fallback_when_empty's FLEET default is runtime-controlled by the
    # `feed-fallback-when-empty-default` flag (issue #651) via _code_engagement_defaults(); the
    # value here is what that flag falls back to. A saved row always wins over both.
    "feed_fallback_when_empty": True, "link_in_first_comment": True,
    # Catch-up congratulations (issue #482): small cap, human approval, and only the BD-relevant
    # milestone types out of the box — a generic "Congrats!" at volume is worse than nothing.
    # The message itself defaults to LinkedIn's own pre-drafted response (no LLM).
    "max_catchup_touches_per_day": CATCHUP_TOUCHES_MAX_STANDARD, "catchup_touch_mode": "pre_review",
    "catchup_event_types": list(DEFAULT_CATCHUP_EVENT_TYPES),
    "catchup_message_source": "linkedin",
    # Per-contact catch-up frequency guard (issue #1078). A new congratulations to the same person is
    # held until `min_catchup_contact_interval_days` have passed since the last one, and at most
    # `max_catchup_touches_per_contact_days` may land per rolling CATCHUP_CONTACT_CAP_WINDOW_DAYS.
    # Both default to small, safe values that rarely block normal usage but stop a burst across
    # multiple milestone types.
    "min_catchup_contact_interval_days": CATCHUP_MIN_CONTACT_INTERVAL_DAYS_DEFAULT,
    "max_catchup_touches_per_contact_days": CATCHUP_MAX_PER_CONTACT_DAYS_DEFAULT,
    "posts_per_week": DEFAULT_POSTS_PER_WEEK,
    "posting_days": list(DEFAULT_POSTING_DAYS),
    # AI image on generated TEXT posts (image-generation overhaul). ON by default — a bare text
    # post is the lowest-reach format; the review queue is still the human gate on every image.
    "text_post_images": True,
    # Opt-in auto-follow of roster targets (issue #962). OFF by default and small when on: bulk
    # following is a classic bot signature, so this only ever runs because the user asked for it.
    "roster_auto_follow": False,
    "max_follows_per_day": ROSTER_FOLLOWS_PER_DAY_DEFAULT,
    # Opt-in auto-connect for roster targets following did not unlock (issue #979). OFF by default
    # and independent of the follow toggle: an invite is heavier and less reversible than a follow,
    # and it spends the account's ONE combined invite budget.
    "roster_auto_connect": False,
}
_ENGAGEMENT_JSON_FIELDS = ("include_topics", "exclude_topics", "include_keywords",
                           "exclude_keywords", "include_authors", "exclude_authors", "post_types",
                           "focus_topics", "connection_target_authors", "catchup_event_types",
                           "posting_days")
_ENGAGEMENT_BOOL_FIELDS = ("use_emojis", "use_hashtags", "reply_to_own_comments",
                           "feed_fallback_when_empty", "link_in_first_comment",
                           "text_post_images", "roster_auto_follow", "roster_auto_connect")
_ENGAGEMENT_COLS = ("tone", "comment_length", "comment_style", "use_emojis", "use_hashtags",
                    "include_topics", "exclude_topics", "include_keywords", "exclude_keywords",
                    "include_authors", "exclude_authors", "post_types", "focus_topics",
                    "business_goals", "personal_goals",
                    "authenticity_score_min", "post_similarity_max_pct", "min_reactions",
                    "max_post_age_hours", "reply_to_own_comments", "max_comments_per_day",
                    "max_dms_per_day", "max_invites_per_day",
                    "max_company_page_invites_per_day", "connection_request_mode",
                    "connection_targeting_mode", "connection_target_authors",
                    "min_connection_icp_score",
                    "default_buyer_stage", "default_video_quality",
                    "reply_check_mode", "reply_sweeps_per_day", "reply_max_post_age_days",
                    "feed_fallback_when_empty", "link_in_first_comment",
                    "max_catchup_touches_per_day", "catchup_touch_mode", "catchup_event_types",
                    "catchup_message_source", "min_catchup_contact_interval_days",
                    "max_catchup_touches_per_contact_days", "posts_per_week", "posting_days",
                    "text_post_images", "roster_auto_follow", "max_follows_per_day",
                    "roster_auto_connect")

VALID_REPLY_MODES = ("event", "scheduled", "off")
# Approval posture for the proactive connect flow (issue #398 owner review).
VALID_CONNECTION_REQUEST_MODES = ("auto_approve", "pre_review")
# Sourcing posture for smart connection targeting (issue #486): 'off' = no sourcing, 'suggest' =
# source but always file as drafts, 'auto_queue' = defer to connection_request_mode.
VALID_CONNECTION_TARGETING_MODES = ("off", "suggest", "auto_queue")
ICP_SCORE_MIN, ICP_SCORE_MAX = 0, 100
# Scheduled reply-sweep cadence bounds: floor 2×/day (as requested), cap 12×/day (every ~2h).
REPLY_SWEEPS_MIN, REPLY_SWEEPS_MAX = 2, 12
REPLY_MAX_AGE_DAYS_MIN, REPLY_MAX_AGE_DAYS_MAX = 1, 14


def _coerce_json_list(value) -> list:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    try:
        parsed = json.loads(value)
        return parsed if isinstance(parsed, list) else []
    except (ValueError, TypeError):
        return []


def _select_engagement_row(user_id: int) -> Optional[dict]:
    """The user's SAVED engagement row, decoded — or None when they have never saved one.

    Deliberately lets `mysql.connector.Error` escape: a read failure is not the same as a missing
    row, and `update_engagement_preferences` must be able to tell them apart before it rewrites
    every column (issue #639).
    """
    connection = _connection.get_db_connection()
    cursor = connection.cursor(dictionary=True)
    try:
        cursor.execute(
            f"SELECT {', '.join(_ENGAGEMENT_COLS)} FROM engagement_preferences WHERE user_id = %s",
            (user_id,))
        row = cursor.fetchone()
        if row is None:
            return None
        # A NULL catchup_event_types (every row predating the V20260724211808 migration) means
        # "never configured" -> the default BD subset. An explicit empty list means the user turned
        # catch-up touches off, so only coerce the NULL case.
        if row.get("catchup_event_types") is None:
            row["catchup_event_types"] = list(DEFAULT_CATCHUP_EVENT_TYPES)
        if row.get("catchup_message_source") not in VALID_CATCHUP_MESSAGE_SOURCES:
            row["catchup_message_source"] = _ENGAGEMENT_DEFAULTS["catchup_message_source"]
        # A NULL cadence (a row written before the posts_per_week migration) means "never chosen",
        # so the planner gets the 3/week default rather than a falsy value it would read as zero.
        if row.get("posts_per_week") is None:
            row["posts_per_week"] = DEFAULT_POSTS_PER_WEEK
        # A NULL company-page invite cap (any row predating the V20260727175938 migration) means
        # "never chosen" -> the conservative default. Reading NULL as 0 would silently switch the
        # lane off for every existing user; an explicit 0 IS "off" and is preserved.
        if row.get("max_company_page_invites_per_day") is None:
            row["max_company_page_invites_per_day"] = COMPANY_PAGE_INVITES_PER_DAY_DEFAULT
        # Same reading for the follow cap (issue #962): NULL is "never chosen" -> the conservative
        # code default. An explicit 0 is the user switching the lane off and is preserved. The
        # TOGGLE is not read this way — it is NOT NULL DEFAULT 0, because "off" and "never chosen"
        # must behave identically for a feature that did not exist yesterday.
        if row.get("max_follows_per_day") is None:
            row["max_follows_per_day"] = ROSTER_FOLLOWS_PER_DAY_DEFAULT
        for f in _ENGAGEMENT_JSON_FIELDS:
            row[f] = _coerce_json_list(row.get(f))
        # A NULL/empty posting_days (any row predating the V20260727045811 migration) means "never
        # chosen" -> Mon-Fri. Unlike catchup_event_types, an empty set here is NOT a meaningful
        # choice: it would leave the planner with no day to publish on at all.
        row["posting_days"] = normalize_posting_days(row.get("posting_days"))
        for f in _ENGAGEMENT_BOOL_FIELDS:
            row[f] = bool(row.get(f))
        return row
    finally:
        cursor.close()
        connection.close()


def _code_engagement_defaults(user_id: int) -> dict:
    """`_ENGAGEMENT_DEFAULTS` with the one field whose FLEET default is runtime-controlled resolved
    for this user (issue #651). Only reached when the user has no saved row: once they save one, the
    column holds their own explicit 0/1 and the flag can never override it.
    """
    from cqc_lem.utilities.flags import FEED_FALLBACK_DEFAULT, flag_enabled
    defaults = dict(_ENGAGEMENT_DEFAULTS)
    defaults["feed_fallback_when_empty"] = flag_enabled(FEED_FALLBACK_DEFAULT, user_id=user_id)
    return defaults


def get_engagement_preferences(user_id: int) -> dict:
    """Return the user's engagement preferences (voice/targeting/caps) with code-level
    defaults when no row exists — so behaviour is unchanged until the user customizes.
    """
    try:
        row = _select_engagement_row(user_id)
    except mysql.connector.Error as err:
        myprint(f"Could not get engagement prefs for user_id {user_id} | Error: {err}")
        return _code_engagement_defaults(user_id)
    return _code_engagement_defaults(user_id) if row is None else row


def engagement_preferences_are_configured(user_id: int) -> Optional[bool]:
    """Whether the user has SAVED an engagement-preferences row of their own.

    The ONE existence check — `has_engagement_preferences` is this function with the unreadable
    case folded back into False, so the question is asked with one query and one semantics.

    Three-valued: None means the row could not be READ, which is NOT the same as "never configured"
    (issue #639). A caller that would otherwise write policy defaults over settings the user chose
    has to be able to tell those two apart (issue #952).
    """
    try:
        with db_cursor() as cursor:
            cursor.execute("SELECT 1 FROM engagement_preferences WHERE user_id = %s LIMIT 1", (user_id,))
            return cursor.fetchone() is not None
    except mysql.connector.Error as err:
        log_error("Could not read engagement prefs — configured state unknown",
                  exc=err, user_id=user_id)
        return None


def update_engagement_preferences(user_id: int, prefs: dict) -> bool:
    """Upsert the user's engagement preferences (INSERT ... ON DUPLICATE KEY UPDATE)."""
    # The upsert writes EVERY column, so a partial `prefs` dict must merge over the user's own
    # SAVED row — merging over `_ENGAGEMENT_DEFAULTS` reset tone/targeting/caps/goals for anyone
    # calling with a single key (issue #639, e.g. set_default_video_quality). Code defaults are
    # the base only for a genuinely new row. An UNREADABLE row aborts the write: overwriting all
    # 39 columns with defaults because a SELECT failed is exactly the data loss being fixed.
    try:
        existing = _select_engagement_row(user_id)
    except mysql.connector.Error as err:
        # ERROR, not myprint: this silently ABORTS the user's save, so it has to reach PostHog
        # rather than sit at INFO under the default POSTHOG_LOG_LEVEL.
        log_error("Could not read engagement prefs before update — aborting write",
                  exc=err, user_id=user_id)
        return False
    base = {**_code_engagement_defaults(user_id),
            **{k: v for k, v in (existing or {}).items() if k in _ENGAGEMENT_DEFAULTS}}
    merged = {**base, **{k: v for k, v in prefs.items() if k in _ENGAGEMENT_DEFAULTS}}

    # Clamp/validate reply-check config so a bad value can't overflow a column and roll back the
    # WHOLE single-row upsert (the V52 tone incident). Bad mode → the safe default; out-of-range
    # numbers → clamped to bounds.
    if merged.get("reply_check_mode") not in VALID_REPLY_MODES:
        merged["reply_check_mode"] = "event"
    if merged.get("connection_request_mode") not in VALID_CONNECTION_REQUEST_MODES:
        merged["connection_request_mode"] = "auto_approve"
    if merged.get("connection_targeting_mode") not in VALID_CONNECTION_TARGETING_MODES:
        merged["connection_targeting_mode"] = "suggest"
    _icp = merged.get("min_connection_icp_score")
    try:
        merged["min_connection_icp_score"] = (min(ICP_SCORE_MAX, max(ICP_SCORE_MIN, int(_icp)))
                                              if _icp is not None else 55)
    except (TypeError, ValueError):
        merged["min_connection_icp_score"] = 55
    # Clamp numerics WITHOUT `or` fallbacks — 0 is falsy but is a real (out-of-range) value that must
    # clamp to the floor, not silently become the default (matches the API-layer validators).
    _sw = merged.get("reply_sweeps_per_day")
    try:
        merged["reply_sweeps_per_day"] = (min(REPLY_SWEEPS_MAX, max(REPLY_SWEEPS_MIN, int(_sw)))
                                          if _sw is not None else REPLY_SWEEPS_MIN)
    except (TypeError, ValueError):
        merged["reply_sweeps_per_day"] = REPLY_SWEEPS_MIN
    _age = merged.get("reply_max_post_age_days")
    try:
        merged["reply_max_post_age_days"] = (min(REPLY_MAX_AGE_DAYS_MAX, max(REPLY_MAX_AGE_DAYS_MIN, int(_age)))
                                             if _age is not None else 2)
    except (TypeError, ValueError):
        merged["reply_max_post_age_days"] = 2
    _ppw = merged.get("posts_per_week")
    try:
        merged["posts_per_week"] = (min(POSTS_PER_WEEK_MAX, max(POSTS_PER_WEEK_MIN, int(_ppw)))
                                    if _ppw is not None else DEFAULT_POSTS_PER_WEEK)
    except (TypeError, ValueError):
        merged["posts_per_week"] = DEFAULT_POSTS_PER_WEEK
    _cpi = merged.get("max_company_page_invites_per_day")
    try:
        merged["max_company_page_invites_per_day"] = (
            min(COMPANY_PAGE_INVITES_PER_DAY_MAX, max(COMPANY_PAGE_INVITES_PER_DAY_MIN, int(_cpi)))
            if _cpi is not None else COMPANY_PAGE_INVITES_PER_DAY_DEFAULT)
    except (TypeError, ValueError):
        merged["max_company_page_invites_per_day"] = COMPANY_PAGE_INVITES_PER_DAY_DEFAULT
    _fol = merged.get("max_follows_per_day")
    try:
        merged["max_follows_per_day"] = (
            min(ROSTER_FOLLOWS_PER_DAY_MAX, max(ROSTER_FOLLOWS_PER_DAY_MIN, int(_fol)))
            if _fol is not None else ROSTER_FOLLOWS_PER_DAY_DEFAULT)
    except (TypeError, ValueError):
        merged["max_follows_per_day"] = ROSTER_FOLLOWS_PER_DAY_DEFAULT
    # The publishing day allow-list (issue #581): de-duped, sorted, Mon..Sun only. Anything
    # unusable — an empty set, strings, out-of-range ints — falls back to Mon-Fri rather than
    # persisting a cadence that would schedule nothing or a value the column would reject.
    merged["posting_days"] = normalize_posting_days(merged.get("posting_days"))
    # Quality-gate thresholds (issue #421): None means "use the deploy default", anything else is
    # clamped to its valid band so an out-of-range slider can never make a gate un-passable.
    from cqc_lem.utilities.quality_gates import (
        AUTHENTICITY_SCORE_MIN_BOUNDS,
        SIMILARITY_MAX_PCT_BOUNDS,
        clamp_threshold,
    )
    merged["authenticity_score_min"] = clamp_threshold(
        merged.get("authenticity_score_min"), *AUTHENTICITY_SCORE_MIN_BOUNDS)
    merged["post_similarity_max_pct"] = clamp_threshold(
        merged.get("post_similarity_max_pct"), *SIMILARITY_MAX_PCT_BOUNDS)
    if merged.get("catchup_touch_mode") not in VALID_CATCHUP_TOUCH_MODES:
        merged["catchup_touch_mode"] = "pre_review"
    if merged.get("catchup_message_source") not in VALID_CATCHUP_MESSAGE_SOURCES:
        merged["catchup_message_source"] = "linkedin"
    # The cap ceiling is per-plan: 10/day only on an active premium plan, 5/day otherwise. Clamped
    # here (not just at the API boundary) so a downgrade silently pulls the saved cap back down.
    _cap_max = max_catchup_touches_allowed(user_id)
    _ct = merged.get("max_catchup_touches_per_day")
    try:
        merged["max_catchup_touches_per_day"] = (
            min(_cap_max, max(CATCHUP_TOUCHES_MIN, int(_ct))) if _ct is not None
            else min(_cap_max, _ENGAGEMENT_DEFAULTS["max_catchup_touches_per_day"]))
    except (TypeError, ValueError):
        merged["max_catchup_touches_per_day"] = min(
            _cap_max, _ENGAGEMENT_DEFAULTS["max_catchup_touches_per_day"])
    # Drop unknown milestone types before they hit the ENUM-validated ledger.
    merged["catchup_event_types"] = [t for t in (merged.get("catchup_event_types") or [])
                                     if t in tuple(CatchupEventType)]
    # Per-contact catch-up frequency guard (issue #1078). 0 disables the guard; otherwise clamp to
    # a sensible band so a malformed value can't lock the lane for a year or make it negative.
    _interval = merged.get("min_catchup_contact_interval_days")
    try:
        merged["min_catchup_contact_interval_days"] = (
            min(CATCHUP_MIN_CONTACT_INTERVAL_DAYS_MAX,
                max(CATCHUP_MIN_CONTACT_INTERVAL_DAYS_MIN, int(_interval)))
            if _interval is not None else CATCHUP_MIN_CONTACT_INTERVAL_DAYS_DEFAULT)
    except (TypeError, ValueError):
        merged["min_catchup_contact_interval_days"] = CATCHUP_MIN_CONTACT_INTERVAL_DAYS_DEFAULT
    _per_contact = merged.get("max_catchup_touches_per_contact_days")
    try:
        merged["max_catchup_touches_per_contact_days"] = (
            min(CATCHUP_MAX_PER_CONTACT_DAYS_MAX,
                max(CATCHUP_MAX_PER_CONTACT_DAYS_MIN, int(_per_contact)))
            if _per_contact is not None else CATCHUP_MAX_PER_CONTACT_DAYS_DEFAULT)
    except (TypeError, ValueError):
        merged["max_catchup_touches_per_contact_days"] = CATCHUP_MAX_PER_CONTACT_DAYS_DEFAULT

    def _val(col):
        v = merged[col]
        if col in _ENGAGEMENT_JSON_FIELDS:
            return json.dumps(v or [])
        if col in _ENGAGEMENT_BOOL_FIELDS:
            return 1 if v else 0
        return v

    values = [user_id] + [_val(c) for c in _ENGAGEMENT_COLS]
    placeholders = ", ".join(["%s"] * (len(_ENGAGEMENT_COLS) + 1))
    updates = ", ".join(f"{c}=VALUES({c})" for c in _ENGAGEMENT_COLS)
    try:
        with db_cursor(commit=True) as cursor:
            cursor.execute(
                f"INSERT INTO engagement_preferences (user_id, {', '.join(_ENGAGEMENT_COLS)}) "
                f"VALUES ({placeholders}) ON DUPLICATE KEY UPDATE {updates}", values)
            return cursor.rowcount >= 0
    except mysql.connector.Error as err:
        myprint(f"Could not update engagement prefs for user_id {user_id} | Error: {err}")
        return False






# TERMINAL for AUTOMATION: one shot per target. 'requested' and 'failed' both mean LinkedIn has our
# one invite (or refused it), and re-inviting someone who declined is the pattern that gets accounts
# restricted — the user decides manually from there. 'connected' is the ladder finishing.
ENGAGEMENT_TARGET_CONNECT_TERMINAL = frozenset({ConnectStatus.REQUESTED, ConnectStatus.CONNECTED,
                                                ConnectStatus.FAILED})
# TERMINAL for CLICKING: the roster pass never spends another follow click on a target that reached
# either. 'follow_failed' is still re-READ on later visits (a read-only correction costs nothing and
# a follow that landed but could not be verified must not be retired forever) — see
# `reconcile_roster_follow_state`.
ENGAGEMENT_TARGET_FOLLOW_TERMINAL = frozenset({FollowStatus.FOLLOWING, FollowStatus.FOLLOW_FAILED})
# Consecutive BLOCKED VISITS before the roster card badges the target. Two distinct visits, not two
# cards on one visit: a single page that happened to render only reshares is not evidence that the
# author restricts commenting.
ENGAGEMENT_TARGET_BLOCKED_BADGE_STREAK = 2
_ENGAGEMENT_TARGET_COLS = ("id", "profile_url", "name", "category", "max_comments_per_week",
                           "active", "last_engaged_at", "comments_this_week", "week_start",
                           "source", "comment_blocked_streak", "last_blocked_at", "follow_status",
                           "followed_at", "follow_attempts", "connect_status", "connect_requested_at")


def resolve_weekly_cap(value: Any) -> int:
    """The per-author weekly cap, with an EXPLICIT 0 preserved. 0 is how the SPA pauses an account
    without removing it, so `value or DEFAULT` would read that pause as "unset" and hand the account
    the default two comments a week — the opposite of what the operator asked for.
    """
    if value is None:
        return ENGAGEMENT_TARGET_WEEKLY_DEFAULT
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return ENGAGEMENT_TARGET_WEEKLY_DEFAULT


def engagement_week_start(today: Optional[date] = None) -> date:
    """Monday of the week `today` falls in — the reset boundary for the per-author weekly cap."""
    today = today or datetime.now().date()
    return today - timedelta(days=today.weekday())


def _clean_target_row(row: dict) -> dict:
    """Normalize a roster row: bools as bools, and a STALE weekly counter reported as 0 so a target
    whose cap was spent last week is immediately eligible again without a reset job.
    """
    row["active"] = bool(row.get("active"))
    if row.get("week_start") != engagement_week_start():
        row["comments_this_week"] = 0
    row["comments_this_week"] = int(row.get("comments_this_week") or 0)
    row["max_comments_per_week"] = resolve_weekly_cap(row.get("max_comments_per_week"))
    row["comment_blocked_streak"] = int(row.get("comment_blocked_streak") or 0)
    row["follow_attempts"] = int(row.get("follow_attempts") or 0)
    if row.get("follow_status") not in ENGAGEMENT_TARGET_FOLLOW_STATUSES:
        row["follow_status"] = FollowStatus.UNKNOWN.value
    if row.get("connect_status") not in ENGAGEMENT_TARGET_CONNECT_STATUSES:
        row["connect_status"] = ConnectStatus.UNKNOWN.value
    return row


def get_engagement_targets(user_id: int, active_only: bool = False) -> list:
    """The user's engagement roster, grouped by category and oldest-configured first within each
    category. `comments_this_week` is already week-aware (0 once the stored week_start is not the
    current week).
    """
    try:
        with db_cursor(dictionary=True) as cursor:
            sql = (f"SELECT {', '.join(_ENGAGEMENT_TARGET_COLS)} FROM engagement_targets "
                   f"WHERE user_id=%s")
            if active_only:
                sql += " AND active=1"
            sql += " ORDER BY category, id"
            cursor.execute(sql, (user_id,))
            return [_clean_target_row(r) for r in (cursor.fetchall() or [])]
    except mysql.connector.Error as err:
        log_error("Could not list engagement targets", exc=err, user_id=user_id)
        return []






def record_target_engagement(user_id: int, profile_url: str) -> bool:
    """Count one comment against a roster author's weekly cap and stamp last_engaged_at. The
    counter resets in the same statement when the stored week_start is not the current week, so a
    new week always starts from 1 without a separate reset job.

    A landed comment also clears `comment_blocked_streak` (issue #962): the streak means "we could
    not comment here", and this IS the proof that we could. Folded into this one statement rather
    than a second call site so the two can never disagree.

    For the same reason a pending 'needs_connection' escalation (issue #979) is stood back down to
    'unknown': it means "following did not unlock commenting", and commenting just worked. Only that
    one state is cleared — an invite already sent ('requested'/'failed'/'connected') is a fact about
    LinkedIn that a comment landing does not undo.
    """
    week = engagement_week_start()
    try:
        with db_cursor(commit=True) as cursor:
            cursor.execute(
                "UPDATE engagement_targets SET "
                "comments_this_week = IF(week_start = %s, comments_this_week + 1, 1), "
                "week_start = %s, last_engaged_at = NOW(), comment_blocked_streak = 0, "
                f"connect_status = IF(connect_status = '{ConnectStatus.NEEDS_CONNECTION.value}', "
                f"'{ConnectStatus.UNKNOWN.value}', connect_status) "
                "WHERE user_id=%s AND profile_url=%s", (week, week, user_id, str(profile_url or "").strip()))
            return cursor.rowcount > 0
    except mysql.connector.Error as err:
        log_error("Could not record target engagement", exc=err, user_id=user_id)
        return False












def suggest_engagement_targets(user_id: int, limit: int = 20) -> list:
    """Seed candidates for an empty roster: people who recently engaged with the user's OWN posts
    (post_engagers), minus anyone already on the roster. Costs no scraping. Suggested as 'icp' —
    someone reacting to your content is far likelier to be a buyer than a peer — and the operator
    re-categorizes in the editor.
    """
    if limit <= 0:
        return []
    existing = {str(t.get("profile_url") or "").rstrip("/").lower()
                for t in get_engagement_targets(user_id)}
    out = []
    for cand in get_engager_candidates(user_id, days=60):
        url = str(cand.get("person_profile_url") or "").strip()
        if not url or url.rstrip("/").lower() in existing:
            continue
        existing.add(url.rstrip("/").lower())
        out.append({"profile_url": url, "name": cand.get("person_name"), "category": "icp",
                    "max_comments_per_week": ENGAGEMENT_TARGET_WEEKLY_DEFAULT,
                    "active": True, "source": "suggested"})
        if len(out) >= limit:
            break
    return out


# What "a seeded bank" means — the onboarding nudge and the SPA both aim the user at this many.
STORY_BANK_TARGET_ENTRIES = 5
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










def get_or_create_reply_inbound_token(user_id: int) -> Optional[str]:
    """The user's PERSISTENT inbound token for the comment-notification forwarding address
    (reply+<token>@parse-domain). Minted once and stored on the users row so the Gmail forward
    filter the user sets up keeps resolving to them. Returns None only on DB error.
    """
    connection = _connection.get_db_connection()
    cursor = connection.cursor()
    try:
        cursor.execute("SELECT reply_inbound_token FROM users WHERE id = %s", (user_id,))
        row = cursor.fetchone()
        if row and row[0]:
            return row[0]
        token = uuid.uuid4().hex[:20]
        cursor.execute("UPDATE users SET reply_inbound_token = %s WHERE id = %s", (token, user_id))
        connection.commit()
        return token
    except mysql.connector.Error as err:
        myprint(f"Could not get/create reply inbound token for user_id {user_id} | Error: {err}")
        return None
    finally:
        cursor.close()
        connection.close()


def get_user_id_by_reply_token(token: str) -> Optional[int]:
    """Reverse lookup for the comment-notification webhook: token → user_id (unique index)."""
    if not token:
        return None
    try:
        with db_cursor() as cursor:
            cursor.execute("SELECT id FROM users WHERE reply_inbound_token = %s", (token,))
            row = cursor.fetchone()
            return row[0] if row else None
    except mysql.connector.Error as err:
        myprint(f"Could not look up user by reply token | Error: {err}")
        return None


def get_users_with_reply_mode(mode: str) -> list:
    """user_ids whose engagement prefs set reply_check_mode = mode (drives the scheduled sweep
    dispatcher). Users with no prefs row default to 'event', so they never appear for 'scheduled'.
    """
    try:
        with db_cursor() as cursor:
            cursor.execute("SELECT user_id FROM engagement_preferences WHERE reply_check_mode = %s", (mode,))
            return [r[0] for r in cursor.fetchall()]
    except mysql.connector.Error as err:
        myprint(f"Could not get users with reply mode {mode} | Error: {err}")
        return []






def count_dms_sent_today(user_id: int) -> int:
    """DMs logged as SUCCESS since the database's own midnight — the counterpart cap to `count_comments_today`."""
    return _count_actions_today(user_id, LogActionType.DM)



# scheduled_dms.source for an auto-drafted DM-nurture reply (issue #485). NULL/absent means an
# operator wrote it by hand, which is what every pre-#485 row is.
SCHEDULED_DM_SOURCE_NURTURE = 'nurture'
# scheduled_dms.source for an approval-gated owned-asset delivery (issue #624) — the lead magnet a
# commenter asked for by keyword. Kept distinct from 'nurture' so each mechanic gets its own daily
# draft cap and its own delivery count; the one-open-draft rule is deliberately SHARED across the
# two (both write to the same thread, so two queued messages would read as spam to one person).
SCHEDULED_DM_SOURCE_ARTIFACT = 'artifact'










def get_scheduled_dm_user_id(dm_id: int) -> Optional[int]:
    """Who owns a scheduled DM, for the API's target-authorisation check (issue #914).

    None for a missing OR unreadable row: callers compare it against the session user, so either way the
    request is denied rather than allowed.
    """
    row = get_scheduled_dm(dm_id)
    return row["user_id"] if row else None
























def get_connection_request_user_id(request_id: int) -> Optional[int]:
    """Who owns a connection request, for the API's target-authorisation check (issue #914).

    None for a missing OR unreadable row, which denies rather than allows.
    """
    row = get_connection_request(request_id)
    return row["user_id"] if row else None












# --- Smart connection targeting (issue #486) — sourcing + dedup for the #398 send path ---





def get_engager_candidates(user_id: int, days: int = 30) -> list:
    """People who recently engaged with the user's OWN posts, as connection-targeting candidates:
    [{'person_name', 'person_profile_url', 'connection_degree', 'occurred_at'}]. Only rows with a
    profile URL — without one there is nobody to invite. Read from post_engagers, so this costs no
    scraping. `connection_degree` lets the caller drop people we're already connected to (#623).
    """
    try:
        with db_cursor(dictionary=True) as cursor:
            cursor.execute(
                "SELECT engager_name AS person_name, engager_profile_url AS person_profile_url, "
                "connection_degree, last_engaged_at AS occurred_at FROM post_engagers "
                "WHERE user_id=%s AND engager_profile_url IS NOT NULL "
                "AND last_engaged_at >= (NOW() - INTERVAL %s DAY) ORDER BY last_engaged_at DESC",
                (user_id, days))
            return cursor.fetchall()
    except mysql.connector.Error as err:
        myprint(f"Could not read engager candidates for user {user_id} | Error: {err}")
        return []








def get_outreach_target_user_id(target_id: int) -> Optional[int]:
    """Who owns an outreach target, for the API's target-authorisation check (issue #914).

    None for a missing OR unreadable row, which denies rather than allows.
    """
    row = get_outreach_target(target_id)
    return row["user_id"] if row else None






















def get_catchup_touch_user_id(touch_id: int) -> Optional[int]:
    """Who owns a catch-up touch, for the API's target-authorisation check (issue #914).

    None for a missing OR unreadable row, which denies rather than allows.
    """
    row = get_catchup_touch(touch_id)
    return row["user_id"] if row else None






















def has_scheduled_post_today(user_id: int) -> bool:
    """True if the user has a post going out today (UTC) — those days are already covered by the
    pre-post commenting trigger, so the standalone daily engagement run should skip them. Fails
    safe to True (skip the standalone run) so an error never causes double-commenting.
    """
    try:
        with db_cursor() as cursor:
            cursor.execute(
                "SELECT COUNT(*) FROM posts WHERE user_id=%s AND DATE(scheduled_time)=UTC_DATE() "
                "AND status IN ('approved','scheduled','posted')", (user_id,))
            r = cursor.fetchone()
            return bool(r and r[0])
    except mysql.connector.Error as err:
        myprint(f"Could not check today's posts for user {user_id} | Error: {err}")
        return True


def upsert_engager(user_id: int, engager_name: str, engager_profile_url: str = None,
                   connection_degree: str = None) -> bool:
    """Record that `engager_name` engaged with the user's post (or refresh their last-engaged
    time). No-op on a blank name or if the table isn't present yet. `connection_degree` is the
    scraped badge ('1st'/'2nd'/'3rd+', issue #623) — COALESCEd, so a later sighting that rendered no
    badge never erases a degree we already know.
    """
    if not engager_name or not engager_name.strip():
        return False
    try:
        with db_cursor(commit=True) as cursor:
            cursor.execute(
                "INSERT INTO post_engagers (user_id, engager_name, engager_profile_url, "
                "connection_degree, last_engaged_at) VALUES (%s,%s,%s,%s,NOW()) ON DUPLICATE KEY UPDATE "
                "engager_profile_url=COALESCE(VALUES(engager_profile_url), engager_profile_url), "
                "connection_degree=COALESCE(VALUES(connection_degree), connection_degree), "
                "last_engaged_at=NOW()",
                (user_id, engager_name.strip()[:255], (engager_profile_url or None),
                 (connection_degree or None)))
            return True
    except mysql.connector.Error as err:
        myprint(f"Could not upsert engager for user {user_id} | Error: {err}")
        return False


def get_recent_engagers(user_id: int, days: int = 14) -> set:
    """Lowercased names of people who recently commented on the user's OWN posts — reciprocity
    targets to prioritize commenting back on. Empty set if the tracking table isn't present yet.
    """
    try:
        with db_cursor() as cursor:
            cursor.execute(
                "SELECT LOWER(engager_name) FROM post_engagers "
                "WHERE user_id=%s AND last_engaged_at >= (NOW() - INTERVAL %s DAY)",
                (user_id, days))
            return {r[0] for r in cursor.fetchall() if r and r[0]}
    except mysql.connector.Error:
        return set()




































def get_shipped_content_for_quality(user_id: int, days: int = 1) -> list:
    """Everything the user SHIPPED in the last `days`, across all three writing surfaces, as the input
    to the nightly content-quality scoring pass (issue #630).

    One function and one connection for three queries on purpose: the scorer treats posts, comments and
    newsletter editions as one stream of writing, and three separate readers would let a surface drift
    out of the window silently. Each row is
    ``{surface, ref_id, text, shipped_on, format_key, authenticity_score, reactions, comments,
    reposts, impressions}`` — the engagement fields are None for a surface that has no per-item stats
    (comments, newsletters) and for a post whose stats have not been captured yet, which is the normal
    case the night it ships.
    """
    window = max(1, int(days))
    connection = _connection.get_db_connection()
    cursor = connection.cursor(dictionary=True)
    rows: list = []
    try:
        # LEFT JOIN: a post shipped tonight has no post_stats row yet and must still be scored — its
        # engagement rate simply reports as unmeasured until the daily scrape catches up.
        cursor.execute(
            "SELECT p.id, p.content, p.archetype, p.authenticity_score, "
            "  DATE(p.scheduled_time) AS shipped_on, "
            "  s.reactions, s.comments, s.reposts, s.impressions "
            "FROM posts p LEFT JOIN post_stats s "
            "  ON s.post_id=p.id AND s.user_id=p.user_id "
            "  AND s.id IN (SELECT MAX(id) FROM post_stats WHERE user_id=%s GROUP BY post_id) "
            "WHERE p.user_id=%s AND p.status=%s AND p.content IS NOT NULL AND p.content <> '' "
            "  AND p.scheduled_time >= (NOW() - INTERVAL %s DAY) "
            "ORDER BY p.scheduled_time DESC",
            (user_id, user_id, PostStatus.POSTED.value, window))
        for r in (cursor.fetchall() or []):
            rows.append({
                "surface": "post", "ref_id": str(r["id"]), "text": r["content"],
                "shipped_on": r["shipped_on"], "format_key": r.get("archetype"),
                "authenticity_score": r.get("authenticity_score"),
                "reactions": r.get("reactions"), "comments": r.get("comments"),
                "reposts": r.get("reposts"), "impressions": r.get("impressions"),
            })

        cursor.execute(
            "SELECT id, message, DATE(created_at) AS shipped_on FROM logs "
            "WHERE user_id=%s AND action_type=%s AND result=%s "
            "  AND message IS NOT NULL AND message <> '' "
            "  AND created_at >= (NOW() - INTERVAL %s DAY) ORDER BY id DESC",
            (user_id, LogActionType.COMMENT.value, LogResultType.SUCCESS.value, window))
        for r in (cursor.fetchall() or []):
            rows.append({
                "surface": "comment", "ref_id": str(r["id"]), "text": r["message"],
                "shipped_on": r["shipped_on"], "format_key": None,
                "authenticity_score": None, "reactions": None, "comments": None,
                "reposts": None, "impressions": None,
            })

        # published_at can be NULL on a row marked published by an older path; scheduled_for is the
        # intended ship day and is NOT NULL, so it is the fallback rather than dropping the edition.
        cursor.execute(
            "SELECT id, body, `format`, DATE(COALESCE(published_at, scheduled_for)) AS shipped_on "
            "FROM newsletter_editions "
            "WHERE user_id=%s AND status='published' AND body IS NOT NULL AND body <> '' "
            "  AND COALESCE(published_at, scheduled_for) >= (NOW() - INTERVAL %s DAY) "
            "ORDER BY id DESC",
            (user_id, window))
        for r in (cursor.fetchall() or []):
            rows.append({
                "surface": "newsletter", "ref_id": str(r["id"]), "text": r["body"],
                "shipped_on": r["shipped_on"], "format_key": r.get("format"),
                "authenticity_score": None, "reactions": None, "comments": None,
                "reposts": None, "impressions": None,
            })
        return rows
    except mysql.connector.Error as err:
        myprint(f"Could not get shipped content for user {user_id} | Error: {err}")
        return rows
    finally:
        cursor.close()
        connection.close()


def record_content_quality_score(user_id: int, score: dict) -> bool:
    """Persist ONE scored piece of content (issue #630). Upsert on (user_id, surface, ref_id) so a
    re-run of the nightly pass refreshes the reading instead of double-counting it — which is what
    makes the weekly rollup's week-over-week comparison stable.

    Every measured column is nullable and is written as NULL when the dimension was not measured; a 0
    would read as "clean" or "no reach" instead of "not scored".
    """
    score = dict(score or {})
    ref_id = str(score.get("ref_id") or "").strip()
    if not ref_id:
        return False
    try:
        with db_cursor(commit=True) as cursor:
            cursor.execute(
                "INSERT INTO content_quality_scores (user_id, surface, ref_id, shipped_on, slop_hard, "
                "  slop_warn, slop_score, similarity, similarity_measure, authenticity_score, "
                "  hook_chars, hook_within_budget, engagement_rate, impressions, detector_score, "
                "  detector_provider, checks) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                "ON DUPLICATE KEY UPDATE shipped_on=VALUES(shipped_on), slop_hard=VALUES(slop_hard), "
                "  slop_warn=VALUES(slop_warn), slop_score=VALUES(slop_score), "
                "  similarity=VALUES(similarity), similarity_measure=VALUES(similarity_measure), "
                "  authenticity_score=VALUES(authenticity_score), hook_chars=VALUES(hook_chars), "
                "  hook_within_budget=VALUES(hook_within_budget), "
                "  engagement_rate=VALUES(engagement_rate), impressions=VALUES(impressions), "
                "  detector_score=VALUES(detector_score), detector_provider=VALUES(detector_provider), "
                "  checks=VALUES(checks), scored_at=CURRENT_TIMESTAMP",
                (user_id, str(score.get("surface") or "")[:20], ref_id[:64], score.get("shipped_on"),
                 score.get("slop_hard"), score.get("slop_warn"), score.get("slop_score"),
                 score.get("similarity"),
                 (str(score.get("similarity_measure"))[:16] if score.get("similarity_measure") else None),
                 score.get("authenticity_score"), score.get("hook_chars"),
                 (None if score.get("hook_within_budget") is None
                  else int(bool(score.get("hook_within_budget")))),
                 score.get("engagement_rate"), score.get("impressions"), score.get("detector_score"),
                 (str(score.get("detector_provider"))[:32] if score.get("detector_provider") else None),
                 json.dumps(score.get("slop_checks") or [])))
            return True
    except mysql.connector.Error as err:
        myprint(f"Could not record content quality score for user {user_id} | Error: {err}")
        return False


def get_content_quality_scores(user_id: int, days: int = 14) -> list:
    """Scored content rows shipped in the last `days`, newest first — the input to the weekly rollup
    and the analytics panel (issue #630). The rollup needs TWO periods, so callers pass twice their
    comparison window.
    """
    try:
        with db_cursor(dictionary=True) as cursor:
            cursor.execute(
                "SELECT surface, ref_id, shipped_on, slop_hard, slop_warn, slop_score, similarity, "
                "  similarity_measure, authenticity_score, hook_chars, hook_within_budget, "
                "  engagement_rate, impressions, detector_score, detector_provider, scored_at "
                "FROM content_quality_scores "
                "WHERE user_id=%s AND shipped_on >= (CURDATE() - INTERVAL %s DAY) "
                "ORDER BY shipped_on DESC, id DESC",
                (user_id, max(1, int(days))))
            return [
                {**r,
                 "slop_score": float(r["slop_score"]) if r.get("slop_score") is not None else None,
                 "similarity": float(r["similarity"]) if r.get("similarity") is not None else None,
                 "engagement_rate": (float(r["engagement_rate"])
                                     if r.get("engagement_rate") is not None else None)}
                for r in (cursor.fetchall() or [])
            ]
    except mysql.connector.Error:
        return []  # table not created yet (or unreadable) — the rollup reports an empty window


















def _group_post_draft_row(row: dict) -> dict:
    row = dict(row)
    for col in ("created_at", "updated_at", "published_at"):
        val = row.get(col)
        row[col] = val.isoformat() if hasattr(val, "isoformat") else val
    return row




def get_open_group_post_draft(user_id: int) -> Optional[dict]:
    """The user's ONE open group-post draft — the row the SPA previews and the weekly publish run
    consumes. None when nothing is waiting.
    """
    try:
        with db_cursor(dictionary=True) as cursor:
            cursor.execute(
                "SELECT id, user_id, group_id, group_name, content, status, created_at, updated_at, "
                "published_at FROM group_post_drafts WHERE user_id=%s AND status=%s "
                "ORDER BY id DESC LIMIT 1",
                (user_id, str(GroupPostDraftStatus.READY)))
            row = cursor.fetchone()
            return _group_post_draft_row(row) if row else None
    except mysql.connector.Error as err:
        log_error("Could not read the open group post draft", exc=err, user_id=user_id)
        return None


def get_group_post_draft(draft_id: int) -> Optional[dict]:
    """One group-post draft by id, normalised for the API, or None when missing or unreadable."""
    try:
        with db_cursor(dictionary=True) as cursor:
            cursor.execute(
                "SELECT id, user_id, group_id, group_name, content, status, created_at, updated_at, "
                "published_at FROM group_post_drafts WHERE id=%s",
                (draft_id,))
            row = cursor.fetchone()
            return _group_post_draft_row(row) if row else None
    except mysql.connector.Error as err:
        log_error("Could not read group post draft", exc=err, task_name="get_group_post_draft")
        return None




def record_post_stats(user_id: int, post_id: int, reactions: Optional[int], comments: Optional[int],
                      reposts: Optional[int] = 0, impressions: Optional[int] = None,
                      saves: Optional[int] = 0) -> bool:
    """Append one engagement snapshot for a post, with the post's SHAPE copied in beside the numbers.

    archetype / hook_style / format / topic / buyer_stage are snapshotted from `posts` at capture time so
    the feedback loop (issue #386) still knows which shape earned these numbers after the post is edited.
    That SELECT is scoped to `(post_id, user_id)`; when it matches nothing the stats row is still written,
    with those columns NULL.
    """
    try:
        with db_cursor(commit=True) as cursor:
        # Snapshot the post's content attributes at capture time so the feedback loop (#386) can
        # learn which shape/topic earned engagement even if the post is later edited.
            cursor.execute(
                "SELECT archetype, hook_style, post_type, topic, buyer_stage "
                "FROM posts WHERE id=%s AND user_id=%s",
                (post_id, user_id))
            row = cursor.fetchone()
            archetype, hook_style, fmt, topic, buyer_stage = row if row else (None, None, None, None, None)
            cursor.execute(
                "INSERT INTO post_stats (user_id, post_id, reactions, comments, reposts, impressions, "
                "saves, archetype, hook_style, `format`, topic, buyer_stage) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (user_id, post_id, int(reactions or 0), int(comments or 0), int(reposts or 0),
                 impressions, int(saves or 0), archetype, hook_style, fmt, topic, buyer_stage))
            return True
    except mysql.connector.Error as err:
        myprint(f"Could not record post stats for user {user_id} | Error: {err}")
        return False


def get_latest_post_stats(user_id: int, post_id: int) -> Optional[dict]:
    """The most recent captured counts for one post, or None when nothing was ever captured.

    `impressions` stays NULL when the capture never read one — the API probe (#645) grades a
    signal it cannot compare as ungraded rather than as a disagreement.
    """
    try:
        with db_cursor(dictionary=True) as cursor:
            cursor.execute(
                "SELECT reactions, comments, reposts, impressions, saves, captured_at "
                "FROM post_stats WHERE user_id=%s AND post_id=%s ORDER BY id DESC LIMIT 1",
                (user_id, post_id))
            return cursor.fetchone()
    except mysql.connector.Error as err:
        myprint(f"Could not read post stats for user {user_id} post {post_id} | Error: {err}")
        return None


def get_recent_posted_post_ids(user_id: int, days: int = 21) -> list:
    """Ids of posts published in the last `days`, FRESHEST first.

    The ordering is the budget policy, not a display choice — see the note in the body. [] on a read
    error, silently.
    """
    try:
        with db_cursor() as cursor:
        # Freshest first: the reply sweep prioritizes golden-hour posts, so a rate-limited or
        # capped session spends its budget on the posts still being distributed (#401).
            cursor.execute(
                "SELECT id FROM posts WHERE user_id=%s AND status='posted' "
                "AND scheduled_time >= (NOW() - INTERVAL %s DAY) ORDER BY scheduled_time DESC", (user_id, days))
            return [r[0] for r in cursor.fetchall()]
    except mysql.connector.Error:
        return []


def get_uncaptured_posted_post_ids(user_id: int, days: int = 90, limit: int = 5) -> list:
    """Posted posts inside the ANALYTICS window that have no `post_stats` row at all (issue #809).

    The stats sweep only walks `get_recent_posted_post_ids`' short window, but the dashboard reads
    90 days — a post whose capture was missed while it was fresh (automation paused, no permalink
    logged yet, a 429) could never be measured afterwards, which is why the analytics rendered a
    shrinking subset of the account's posts. Newest first and capped, so topping the sweep up costs
    a bounded number of extra page loads.

    Only posts with a logged permalink are offered. The sweep can do nothing with the others, and
    since a post leaves this set only by GAINING a stat row, a handful of permalink-less posts at
    the head of the window would otherwise hold every slot of the cap on every run — the backfill
    would report as working while never reaching a post it could actually capture.
    """
    try:
        with db_cursor() as cursor:
            cursor.execute(
                "SELECT p.id FROM posts p "
                "LEFT JOIN post_stats s ON s.post_id = p.id AND s.user_id = p.user_id "
                "WHERE p.user_id = %s AND p.status = %s "
                "AND p.scheduled_time >= (NOW() - INTERVAL %s DAY) AND s.id IS NULL "
                "AND EXISTS (SELECT 1 FROM logs l WHERE l.user_id = p.user_id AND l.post_id = p.id "
                "AND l.action_type = %s AND l.result = %s "
                "AND l.post_url IS NOT NULL AND l.post_url <> '') "
                "ORDER BY p.scheduled_time DESC LIMIT %s",
                (user_id, PostStatus.POSTED.value, days, LogActionType.POST.value,
                 LogResultType.SUCCESS.value, max(0, int(limit))))
            return [r[0] for r in (cursor.fetchall() or [])]
    except mysql.connector.Error as err:
        myprint(f"Could not get uncaptured posted post ids for user {user_id} | Error: {err}")
        return []


def get_post_coverage_counts(user_id: int, days: int = 90) -> dict:
    """How much of the account the analytics dashboard is looking at (issue #809).

    Three unrelated denominators used to share one screen with no way to reconcile them: the
    all-time "posted" tile, the content-mix window, and the per-post table (which only sees posts
    with a captured `post_stats` row). This returns the two POST-side numbers — all-time posted and
    posted within the analytics window — so the UI can say WHY it is showing a subset instead of
    reading as broken. The measured count stays with the stats read (`get_post_performance_rows`),
    so the panel can never contradict its own sample size.
    """
    try:
        with db_cursor() as cursor:
            cursor.execute(
                "SELECT COALESCE(SUM(status = %s), 0), "
                "COALESCE(SUM(status = %s AND scheduled_time >= (NOW() - INTERVAL %s DAY)), 0) "
                "FROM posts WHERE user_id = %s",
                (PostStatus.POSTED.value, PostStatus.POSTED.value, days, user_id))
            row = cursor.fetchone() or (0, 0)
            return {"posted_total": int(row[0] or 0), "posted_in_window": int(row[1] or 0)}
    except mysql.connector.Error as err:
        myprint(f"Could not get post coverage counts for user {user_id} | Error: {err}")
        return {"posted_total": 0, "posted_in_window": 0}


def get_post_engagement_rows(user_id: int) -> list:
    """Latest stats per post joined with when it was posted → rows of
    (scheduled_time, reactions, comments, reposts, archetype, hook_style, format, topic,
    buyer_stage, impressions) for post-time and content-attribution analysis (#386). The
    attribution columns are the snapshot captured on the stat row, so they reflect the post as it
    was when scraped. `impressions` may be NULL (only the author's own view exposes it) — it
    trails the tuple so index-based readers of the older shape keep working, and it lets
    `post_stats` score by engagement RATE when coverage is complete (#388).
    """
    try:
        with db_cursor() as cursor:
            cursor.execute(
                "SELECT p.scheduled_time, s.reactions, s.comments, s.reposts, "
                "s.archetype, s.hook_style, s.`format`, s.topic, s.buyer_stage, s.impressions "
                "FROM posts p JOIN post_stats s ON s.post_id=p.id AND s.user_id=p.user_id "
                "WHERE p.user_id=%s AND s.id IN "
                "(SELECT MAX(id) FROM post_stats WHERE user_id=%s GROUP BY post_id)",
                (user_id, user_id))
            return cursor.fetchall() or []
    except mysql.connector.Error as err:
        myprint(f"Could not get post engagement rows for user {user_id} | Error: {err}")
        return []


def get_shape_performance(user_id: int, days: int = 90) -> dict:
    """Per-SHAPE engagement totals for a user's recently posted content — the outcomes side of the
    performance→content feedback loop (issue #389 / B4). Joins each posted post's assigned shape
    (`posts.archetype` = short-form FORMAT key, `posts.hook_style`) with its LATEST captured
    `post_stats` row and aggregates raw signal counts per shape key.

    Returns ``{"format": {archetype: agg}, "hook": {hook_style: agg}}`` where each ``agg`` is
    ``{"samples", "reactions", "comments", "reposts", "impressions", "impression_samples"}``.
    ``impressions`` sums only rows where impressions is non-NULL (``impression_samples`` counts
    them) so the caller can tell whether impression-normalized scoring is available yet (B2/B3).
    The engagement-metric/weighting policy lives in ``content_framework``; this stays pure access.
    """
    result = {"format": {}, "hook": {}}
    try:
        with db_cursor() as cursor:
            for column, bucket in (("archetype", "format"), ("hook_style", "hook")):
                cursor.execute(
                    f"SELECT p.{column}, COUNT(*), "
                    "COALESCE(SUM(s.reactions),0), COALESCE(SUM(s.comments),0), "
                    "COALESCE(SUM(s.reposts),0), COALESCE(SUM(s.impressions),0), "
                    "SUM(CASE WHEN s.impressions IS NOT NULL THEN 1 ELSE 0 END) "
                    "FROM posts p JOIN post_stats s "
                    "ON s.post_id=p.id AND s.user_id=p.user_id "
                    f"WHERE p.user_id=%s AND p.status='posted' AND p.{column} IS NOT NULL "
                    "AND p.scheduled_time >= (NOW() - INTERVAL %s DAY) "
                    "AND s.id IN (SELECT MAX(id) FROM post_stats WHERE user_id=%s GROUP BY post_id) "
                    f"GROUP BY p.{column}",
                    (user_id, days, user_id))
                for key, samples, reactions, comments, reposts, impressions, imp_samples in cursor.fetchall():
                    result[bucket][key] = {
                        "samples": int(samples or 0),
                        "reactions": int(reactions or 0),
                        "comments": int(comments or 0),
                        "reposts": int(reposts or 0),
                        "impressions": int(impressions or 0),
                        "impression_samples": int(imp_samples or 0),
                    }
            return result
    except mysql.connector.Error as err:
        myprint(f"Could not get shape performance for user {user_id} | Error: {err}")
        return {"format": {}, "hook": {}}


def get_post_performance_rows(user_id: int, days: Optional[int] = None) -> list:
    """Latest captured stat per POSTED post as attribution-tagged dicts for the analytics
    dashboard (issue #395) — the per-post performance table and the engagement-rate/impression
    trend both read this. Like ``get_post_engagement_rows`` it keeps only the newest stat row per
    post (``MAX(id)``), but returns dicts carrying ``post_id`` and ``saves`` so the UI can key each
    row and surface the save signal (#387). ``impressions`` may be NULL (only the author's own view
    exposes it). ``days`` optionally windows to posts scheduled within the last N days (None = all),
    newest first.
    """
    try:
        with db_cursor() as cursor:
            window = "AND p.scheduled_time >= (NOW() - INTERVAL %s DAY) " if days is not None else ""
            params = (user_id, user_id, days) if days is not None else (user_id, user_id)
            cursor.execute(
                "SELECT p.id, p.scheduled_time, s.reactions, s.comments, s.reposts, s.impressions, "
                "s.saves, s.archetype, s.hook_style, s.`format`, s.topic, s.buyer_stage "
                "FROM posts p JOIN post_stats s ON s.post_id=p.id AND s.user_id=p.user_id "
                "WHERE p.user_id=%s AND p.status='posted' "
                "AND s.id IN (SELECT MAX(id) FROM post_stats WHERE user_id=%s GROUP BY post_id) "
                + window +
                "ORDER BY p.scheduled_time DESC",
                params)
            return [
                {"post_id": r[0], "scheduled_time": r[1], "reactions": r[2], "comments": r[3],
                 "reposts": r[4], "impressions": r[5], "saves": r[6], "archetype": r[7],
                 "hook_style": r[8], "format": r[9], "topic": r[10], "buyer_stage": r[11]}
                for r in (cursor.fetchall() or [])
            ]
    except mysql.connector.Error as err:
        myprint(f"Could not get post performance rows for user {user_id} | Error: {err}")
        return []


def record_shipped_variant(user_id: int, post_id: int, variant_key: str,
                           combo: Optional[dict] = None, batch_id: Optional[str] = None,
                           variant_index: Optional[int] = None) -> bool:
    """Persist which A/B variant actually SHIPPED for a post (issue #396 / D2) so its realized
    `post_stats` can be attributed back to that variant when picking winners. One row per post —
    re-recording overwrites. `combo` is stored as JSON for provenance.
    """
    try:
        with db_cursor(commit=True) as cursor:
            cursor.execute(
                "INSERT INTO post_variants (user_id, post_id, batch_id, variant_index, variant_key, combo) "
                "VALUES (%s,%s,%s,%s,%s,%s) "
                "ON DUPLICATE KEY UPDATE batch_id=VALUES(batch_id), variant_index=VALUES(variant_index), "
                "variant_key=VALUES(variant_key), combo=VALUES(combo), shipped_at=CURRENT_TIMESTAMP",
                (user_id, post_id, batch_id, variant_index, variant_key,
                 json.dumps(combo, default=str) if combo is not None else None))
            return True
    except mysql.connector.Error as err:
        myprint(f"Could not record shipped variant for user {user_id} | Error: {err}")
        return False


def get_shipped_variant_keys(user_id: int) -> dict:
    """``{post_id: variant_key}`` for every A/B variant this user has SHIPPED (issue #396).

    Read once per stats sweep so each `post_outcome` event can carry the variant it belongs to
    (issue #652) — the per-post alternative would be one query per post inside the Selenium loop.
    An empty dict on any DB error: a missing experiment label must never cost us the outcome.
    """
    try:
        with db_cursor() as cursor:
            cursor.execute("SELECT post_id, variant_key FROM post_variants WHERE user_id=%s", (user_id,))
            return {r[0]: r[1] for r in (cursor.fetchall() or []) if r[1]}
    except mysql.connector.Error as err:
        myprint(f"Could not get shipped variant keys for user {user_id} | Error: {err}")
        return {}


def get_variant_outcome_rows(user_id: int) -> list:
    """Realized outcomes for shipped A/B variants (issue #396 / D2). Joins each recorded shipped
    variant (`post_variants`) with its post's LATEST captured `post_stats` row → dicts of
    ``{variant_key, scheduled_time, reactions, comments, reposts, impressions}`` that feed
    ``post_stats.select_variant_winners``. `impressions` may be NULL (only the author's own view
    exposes it), so winner selection falls back to raw counts until coverage is complete.
    """
    try:
        with db_cursor() as cursor:
            cursor.execute(
                "SELECT v.variant_key, p.scheduled_time, s.reactions, s.comments, s.reposts, s.impressions "
                "FROM post_variants v "
                "JOIN posts p ON p.id=v.post_id AND p.user_id=v.user_id "
                "JOIN post_stats s ON s.post_id=v.post_id AND s.user_id=v.user_id "
                "WHERE v.user_id=%s AND s.id IN "
                "(SELECT MAX(id) FROM post_stats WHERE user_id=%s GROUP BY post_id)",
                (user_id, user_id))
            return [
                {"variant_key": r[0], "scheduled_time": r[1], "reactions": r[2],
                 "comments": r[3], "reposts": r[4], "impressions": r[5]}
                for r in (cursor.fetchall() or [])
            ]
    except mysql.connector.Error as err:
        myprint(f"Could not get variant outcome rows for user {user_id} | Error: {err}")
        return []


























# --- lead scoring & CRM-lite pipeline (issue #484) ---------------------------------------------
# Every source below is engagement we ALREADY record, read back as one normalized activity stream
# (kind, person_name, person_profile_url, occurred_at, detail). No new scraping.
_LEAD_ACTIVITY_SOURCES: tuple = (
    (LeadSignalKind.ENGAGED,
     "SELECT engager_name AS person_name, engager_profile_url AS person_profile_url, "
     "last_engaged_at AS occurred_at, '' AS detail FROM post_engagers "
     "WHERE user_id=%s AND last_engaged_at >= (NOW() - INTERVAL %s DAY)"),
    (LeadSignalKind.INTENT,
     "SELECT person_name, person_profile_url, created_at AS occurred_at, status AS detail "
     "FROM lead_signals WHERE user_id=%s AND created_at >= (NOW() - INTERVAL %s DAY) "
     "AND status <> 'dismissed'"),
    (LeadSignalKind.DM,
     "SELECT recipient_name AS person_name, recipient_profile_url AS person_profile_url, "
     "updated_at AS occurred_at, status AS detail FROM scheduled_dms "
     "WHERE user_id=%s AND status='sent' AND updated_at >= (NOW() - INTERVAL %s DAY)"),
    (LeadSignalKind.DM,
     "SELECT first_name AS person_name, profile_url AS person_profile_url, "
     "created_at AS occurred_at, event_type AS detail FROM dm_followups "
     "WHERE user_id=%s AND event_type <> 'profile_viewer' "
     "AND created_at >= (NOW() - INTERVAL %s DAY)"),
    (LeadSignalKind.PROFILE_VIEW,
     "SELECT first_name AS person_name, profile_url AS person_profile_url, "
     "created_at AS occurred_at, event_type AS detail FROM dm_followups "
     "WHERE user_id=%s AND event_type='profile_viewer' "
     "AND created_at >= (NOW() - INTERVAL %s DAY)"),
    (LeadSignalKind.CONNECT,
     "SELECT recipient_name AS person_name, recipient_profile_url AS person_profile_url, "
     "updated_at AS occurred_at, status AS detail FROM connection_requests "
     "WHERE user_id=%s AND status='sent' AND updated_at >= (NOW() - INTERVAL %s DAY)"),
    (LeadSignalKind.FUNNEL,
     "SELECT target_name AS person_name, target_profile_url AS person_profile_url, "
     "updated_at AS occurred_at, stage AS detail FROM outreach_funnel_targets "
     "WHERE user_id=%s AND status <> 'canceled' AND updated_at >= (NOW() - INTERVAL %s DAY)"),
)



def get_lead_activity(user_id: int, days: int = 90) -> list:
    """Every engagement signal about every person who touched this user in the window, normalized
    for the scorer. Each source is queried independently so one unavailable table degrades that
    signal instead of losing the whole pipeline.
    """
    connection = _connection.get_db_connection()
    cursor = connection.cursor(dictionary=True)
    rows: list = []
    try:
        for kind, sql in _LEAD_ACTIVITY_SOURCES:
            try:
                cursor.execute(sql, (user_id, days))
                for row in cursor.fetchall():
                    row["kind"] = str(kind)
                    rows.append(row)
            except mysql.connector.Error as err:
                myprint(f"Could not read {kind} lead activity for user {user_id} | Error: {err}")
        return rows
    finally:
        cursor.close()
        connection.close()


def _profile_url_variants(profile_url: str) -> list:
    """Every spelling of one profile URL worth looking up. Activity rows carry tracking
    querystrings and inconsistent trailing slashes (`/in/jane?trk=feed` vs `/in/jane/`) while
    `profiles` stores whichever form the scraper saw, so an exact match would miss most people —
    same reason get_linked_in_profile_by_url() queries both slash variants.
    """
    raw = str(profile_url or "").strip()
    if not raw:
        return []
    base = raw.split("#", 1)[0].split("?", 1)[0].rstrip("/")
    if not base:
        return [raw]
    return list(dict.fromkeys([raw, base, base + "/"]))


def get_profile_facts(profile_urls: list) -> dict:
    """ICP facts (title / company / industry) for the profiles we HAVE scraped, keyed by the
    profile URL as stored in `profiles` (callers match on the /in/ slug, not the raw string).
    People we never scraped simply aren't in the result — the scorer treats them as neutral.
    """
    urls = list(dict.fromkeys(v for u in (profile_urls or []) if u
                              for v in _profile_url_variants(u)))
    if not urls:
        return {}
    try:
        with db_cursor(dictionary=True) as cursor:
            placeholders = ", ".join(["%s"] * len(urls))
            cursor.execute(
                "SELECT profile_url, "
                "JSON_UNQUOTE(JSON_EXTRACT(data, '$.job_title')) AS job_title, "
                "JSON_UNQUOTE(JSON_EXTRACT(data, '$.company_name')) AS company_name, "
                "JSON_UNQUOTE(JSON_EXTRACT(data, '$.industry')) AS industry "
                f"FROM profiles WHERE profile_url IN ({placeholders})", tuple(urls))
            return {r["profile_url"]: r for r in cursor.fetchall() if r.get("profile_url")}
    except mysql.connector.Error as err:
        myprint(f"Could not read profile facts | Error: {err}")
        return {}






























def _like_literal(value: str, escape: str = "!") -> str:
    """Escape LIKE metacharacters so a value is matched literally. A newsletter URL can carry
    percent-encoding ('%20'), and an unescaped '%' inside the pattern matches ANY text — which would
    silently over-count the attribution it feeds.
    """
    return (str(value).replace(escape, escape + escape)
            .replace("%", escape + "%").replace("_", escape + "_"))


def count_artifact_cta_deliveries(user_id: int, days: int = 90,
                                  newsletter_url: Optional[str] = None) -> dict:
    """Owned-asset CTA deliveries in the last `days` (issue #624) — the attribution half of the loop,
    so subscriber growth can be read against the CTAs that were actually delivered.

    The two mechanics are counted SEPARATELY because they deliver differently and one of them is not
    a send at all: `lead_magnet_dms` counts the approval-gated DM drafts this automation queued, and
    `newsletter_links` counts the published posts that carried the subscribe URL. `newsletter_links`
    is None — not 0 — when the user has no newsletter URL configured: there was nothing to carry,
    which is a different fact from "carried nothing".

    The link side matches EITHER column, because which one holds the URL depends on the host and
    only one of the two cases is the common one: `newsletter_url` is written by
    `mark_newsletter_published` from a linkedin.com article URL, and #392's split deliberately leaves
    in-platform links in the BODY (they carry no reach penalty), so `first_comment_link` alone would
    report 0 forever for the mainline LinkedIn newsletter. An off-platform newsletter (Substack &c.)
    is the reverse: the split moves it out of `content` and into `first_comment_link`.
    """
    window = max(1, int(days or 1))
    out: dict = {"window_days": window, "lead_magnet_dms": 0, "newsletter_links": None}
    try:
        with db_cursor() as cursor:
            cursor.execute(
                "SELECT COUNT(*) FROM scheduled_dms WHERE user_id = %s AND source = %s "
                "AND created_at >= (NOW() - INTERVAL %s DAY)",
                (user_id, SCHEDULED_DM_SOURCE_ARTIFACT, window))
            row = cursor.fetchone()
            out["lead_magnet_dms"] = int(row[0]) if row and row[0] else 0
            url = str(newsletter_url or "").strip()
            if url:
                pattern = f"%{_like_literal(url)}%"
                cursor.execute(
                    "SELECT COUNT(*) FROM posts WHERE user_id = %s AND status = %s "
                    "AND (content LIKE %s ESCAPE '!' OR first_comment_link LIKE %s ESCAPE '!') "
                    "AND updated_at >= (NOW() - INTERVAL %s DAY)",
                    (user_id, PostStatus.POSTED.value, pattern, pattern, window))
                row = cursor.fetchone()
                out["newsletter_links"] = int(row[0]) if row and row[0] else 0
            return out
    except mysql.connector.Error as err:
        myprint(f"Could not count artifact CTA deliveries for user {user_id} | Error: {err}")
        return out


def record_follower_stat(user_id: int, follower_count: Optional[int] = None,
                         connection_count: Optional[int] = None,
                         profile_views: Optional[int] = None,
                         search_appearances: Optional[int] = None) -> bool:
    """Append one audience snapshot for the user (issue #627). Every count is optional: a value the
    capture could not read is stored as NULL, never 0, so the growth deltas can tell "not measured"
    apart from "the audience really is that size". Returns False when NOTHING was readable — there
    is no point writing an all-NULL row that only adds noise to the series.
    """
    if all(v is None for v in (follower_count, connection_count, profile_views, search_appearances)):
        return False
    try:
        with db_cursor(commit=True) as cursor:
            cursor.execute(
                "INSERT INTO follower_stats (user_id, follower_count, connection_count, profile_views, "
                "search_appearances) VALUES (%s, %s, %s, %s, %s)",
                (user_id, follower_count, connection_count, profile_views, search_appearances))
            return cursor.rowcount == 1
    except mysql.connector.Error as err:
        myprint(f"Could not record follower stat for user {user_id} | Error: {err}")
        return False


def get_follower_stats(user_id: int, days: Optional[int] = None, limit: int = 400) -> list:
    """The user's audience snapshots, most recent first (issue #627). `days` optionally windows to
    captures within the last N days. Each item:
    id, follower_count, connection_count, profile_views, search_appearances, captured_at.
    """
    try:
        with db_cursor(dictionary=True) as cursor:
            window = "AND captured_at >= (NOW() - INTERVAL %s DAY) " if days is not None else ""
            params = (user_id, days, limit) if days is not None else (user_id, limit)
            cursor.execute(
                "SELECT id, follower_count, connection_count, profile_views, search_appearances, "
                "captured_at FROM follower_stats WHERE user_id = %s " + window +
                "ORDER BY captured_at DESC, id DESC LIMIT %s", params)
            return cursor.fetchall() or []
    except mysql.connector.Error as err:
        myprint(f"Could not get follower stats for user {user_id} | Error: {err}")
        return []














































# The appreciation triggers that share `_dispatch_appreciation_dms` — and therefore the ledger below.
APPRECIATION_EVENT_TYPES = ("connection_accepted", "recommendation_received", "collaboration")




def count_existing_double_sent_catchups() -> int:
    """Count contacts who were sent the SAME catch-up congratulations more than once.

    Measured off `logs`, not `catchup_touches`: the ledger carries a UNIQUE key on
    (user, profile_url, event_type, event_period), so grouping IT by that key can never return a
    duplicate — the historical double-send this issue is about came from ONE touch row being sent
    twice (a retry or an orphan re-queue after the status update was lost), which shows up only as
    two `success` DM log rows carrying the same body to the same person.

    Read-only, run once at deploy time to report the historical duplicate surface on the issue
    (#1078). Returns 0 when nothing is double-sent or the read fails.
    """
    try:
        with db_cursor() as cursor:
            cursor.execute(
                "SELECT COUNT(*) FROM ("
                "SELECT l.user_id, l.post_url, l.message FROM logs l "
                "WHERE l.action_type = 'dm' AND l.result = 'success' "
                # EXISTS rather than a JOIN: two milestones can share one body (the deterministic
                # fallback congratulations), and a join would multiply ONE log row into a fake duplicate.
                "AND EXISTS (SELECT 1 FROM catchup_touches c WHERE c.user_id = l.user_id "
                "AND c.profile_url = l.post_url AND c.message = l.message) "
                "GROUP BY l.user_id, l.post_url, l.message HAVING COUNT(*) > 1"
                ") dupes")
            r = cursor.fetchone()
            return int(r[0]) if r else 0
    except mysql.connector.Error as err:
        myprint(f"Could not count existing double-sent catch-ups | Error: {err}")
        return 0
















def get_user_geo(user_id: int) -> Optional[dict]:
    """Return the user's full geo profile for Selenium spoofing.

    Keys: latitude, longitude (floats or None), timezone, locale, city, country.
    Returns None only if the user row is missing.
    """
    try:
        with db_cursor() as cursor:
            cursor.execute(
                "SELECT latitude, longitude, timezone, locale, city, country FROM users WHERE id = %s",
                (user_id,),
            )
            row = cursor.fetchone()
    except mysql.connector.Error as err:
        myprint(f"Could not get user geo for user_id {user_id} | Error: {err}")
        row = None
    if not row:
        return None
    return {
        "latitude": float(row[0]) if row[0] is not None else None,
        "longitude": float(row[1]) if row[1] is not None else None,
        "timezone": row[2],
        "locale": row[3],
        "city": row[4],
        "country": row[5],
    }


def get_user_content_language(user_id: Optional[int]) -> str:
    """The BCP-47 language generated content must be produced in (issue #548).

    Precedence: the explicit users.content_language setting → the Login Location locale
    (users.locale) → 'en-US'. The explicit setting wins because location is not language:
    a US-based user may publish in Spanish.
    """
    from cqc_lem.utilities.geocoding import DEFAULT_CONTENT_LANGUAGE
    if not user_id:
        return DEFAULT_CONTENT_LANGUAGE
    # Fail-soft on the connection too: callers sit inside media-generation try/except blocks that
    # degrade to stock footage, so a DB blip here must not cost the user their generated video.
    try:
        connection = _connection.get_db_connection()
    except Exception as err:
        myprint(f"Could not get content language for user_id {user_id} | Error: {err}")
        return DEFAULT_CONTENT_LANGUAGE
    cursor = connection.cursor()
    try:
        cursor.execute("SELECT content_language, locale FROM users WHERE id = %s", (user_id,))
        row = cursor.fetchone()
    except mysql.connector.Error as err:
        myprint(f"Could not get content language for user_id {user_id} | Error: {err}")
        row = None
    finally:
        cursor.close()
        connection.close()
    if not row:
        return DEFAULT_CONTENT_LANGUAGE
    return (row[0] or "").strip() or (row[1] or "").strip() or DEFAULT_CONTENT_LANGUAGE


def update_user_location(user_id: int, latitude: float, longitude: float,
                         city: Optional[str] = None, country: Optional[str] = None,
                         locale: Optional[str] = None, timezone: Optional[str] = None,
                         source: str = "manual") -> bool:
    """Persist the user's location. timezone is updated only when provided so the
    user's display-timezone preference is preserved unless autocapture supplies one.
    """
    try:
        with db_cursor(commit=True) as cursor:
            if timezone:
                cursor.execute(
                    "UPDATE users SET latitude=%s, longitude=%s, city=%s, country=%s, "
                    "locale=%s, timezone=%s, location_source=%s WHERE id=%s",
                    (latitude, longitude, city, country, locale, timezone, source, user_id),
                )
            else:
                cursor.execute(
                    "UPDATE users SET latitude=%s, longitude=%s, city=%s, country=%s, "
                    "locale=%s, location_source=%s WHERE id=%s",
                    (latitude, longitude, city, country, locale, source, user_id),
                )
            return cursor.rowcount >= 0
    except mysql.connector.Error as err:
        myprint(f"Could not update location for user_id {user_id} | Error: {err}")
        return False


def get_user_proxy(user_id: int) -> Optional[str]:
    """Return the user's egress proxy URL (scheme://[user:pass@]host:port) or None.

    Used by Selenium to route a user's browser session through an IP near where they
    normally log in, reducing LinkedIn "new location" challenges. None = egress from
    the host directly.
    """
    try:
        with db_cursor() as cursor:
            cursor.execute("SELECT proxy_url FROM users WHERE id = %s", (user_id,))
            row = cursor.fetchone()
    except mysql.connector.Error as err:
        myprint(f"Could not get proxy for user_id {user_id} | Error: {err}")
        row = None
    if not row or not row[0]:
        return None
    return row[0]


def update_user_proxy(user_id: int, proxy_url: Optional[str]) -> bool:
    """Set (or clear, when proxy_url is None/empty) the user's egress proxy URL."""
    try:
        with db_cursor(commit=True) as cursor:
            cursor.execute(
                "UPDATE users SET proxy_url = %s WHERE id = %s",
                (proxy_url or None, user_id),
            )
            return cursor.rowcount >= 0
    except mysql.connector.Error as err:
        myprint(f"Could not update proxy for user_id {user_id} | Error: {err}")
        return False


def get_user_timezone(user_id: int) -> str:
    """Return the IANA timezone string for the user. Defaults to America/New_York to match the
    users.timezone column default and the UI default (not UTC, which would misrender local times).
    """
    try:
        with db_cursor() as cursor:
            cursor.execute("SELECT timezone FROM users WHERE id = %s", (user_id,))
            row = cursor.fetchone()
            return row[0] if row and row[0] else 'America/New_York'
    except mysql.connector.Error as err:
        myprint(f"Could not get timezone for user_id {user_id} | Error: {err}")
        return 'America/New_York'


def update_user_timezone(user_id: int, tz: str) -> bool:
    """Persist the user's preferred IANA timezone string."""
    try:
        with db_cursor(commit=True) as cursor:
            cursor.execute("UPDATE users SET timezone = %s WHERE id = %s", (tz, user_id))
            return cursor.rowcount >= 0
    except mysql.connector.Error as err:
        myprint(f"Could not update timezone for user_id {user_id} | Error: {err}")
        return False


# ---------------------------------------------------------------------------
# Avatar credit ledger
# ---------------------------------------------------------------------------

def get_user_by_stripe_customer_id(stripe_customer_id: str) -> Optional[dict]:
    """Return the user row matching a Stripe customer ID, regardless of subscription status."""
    try:
        with db_cursor(dictionary=True) as cursor:
            cursor.execute(
                "SELECT id, stripe_customer_id FROM users WHERE stripe_customer_id = %s LIMIT 1",
                (stripe_customer_id,),
            )
            return cursor.fetchone()
    except mysql.connector.Error as err:
        myprint(f"Could not look up user by stripe_customer_id={stripe_customer_id} | Error: {err}")
        return None












# ---------------------------------------------------------------------------
# Premium video credits (mirrors avatar_credit_ledger; balance = SUM(delta))
# ---------------------------------------------------------------------------











def get_post_video_quality(post_id: int) -> str:
    """A post's video-quality tier.

    Every unknown answer — column unset, no such post, failed read — is 'standard': the tier that costs
    credits is never something we assume.
    """
    try:
        with db_cursor(dictionary=True) as cursor:
            cursor.execute("SELECT video_quality FROM posts WHERE id = %s", (post_id,))
            row = cursor.fetchone()
            return (row["video_quality"] if row and row.get("video_quality") else "standard")
    except mysql.connector.Error as err:
        myprint(f"Could not get video_quality for post {post_id} | Error: {err}")
        return "standard"


def update_post_video_quality(post_id: int, quality: str) -> bool:
    """Set a post's video-quality tier.

    False when no row matched or the value stored was already this one.
    """
    try:
        with db_cursor(dictionary=True, commit=True) as cursor:
            cursor.execute("UPDATE posts SET video_quality = %s WHERE id = %s", (quality, post_id))
            return cursor.rowcount > 0
    except mysql.connector.Error as err:
        myprint(f"Could not update video_quality for post {post_id} | Error: {err}")
        return False


def get_post_carousel_slides(post_id: int):
    """The RAW `carousel_slides` column for a post — the stored JSON, not a list.

    `get_carousel_slides` is the parsed reader; this one hands back whatever the column holds (or None),
    so a caller that iterates it will walk a JSON string character by character.
    """
    try:
        with db_cursor(dictionary=True) as cursor:
            cursor.execute("SELECT carousel_slides FROM posts WHERE id = %s", (post_id,))
            row = cursor.fetchone()
            return row["carousel_slides"] if row else None
    except mysql.connector.Error as err:
        myprint(f"Could not get carousel_slides for post {post_id} | Error: {err}")
        return None


def get_unposted_posts_missing_assets(within_days: int = 14) -> list:
    """Posts not yet posted, due within `within_days`, whose required media asset is
    missing: video posts with no video_url, or carousel posts with no slides. Used by the
    backfill safety net. Returns (id, user_id, post_type, buyer_stage, scheduled_time).
    """
    try:
        with db_cursor() as cursor:
        # Include 'error' so failed posts get a regeneration attempt. A carousel needs
        # regeneration when its slides are empty OR are plain text titles with no real image
        # reference — real slides are stored as URLs (https .../api/assets/...png), so the
        # absence of any image marker means generation never produced images.
            cursor.execute("""
                SELECT id, user_id, post_type, buyer_stage, scheduled_time
                FROM posts
                WHERE status IN ('approved', 'pending', 'scheduled', 'error')
                  AND scheduled_time > NOW()
                  AND scheduled_time <= NOW() + INTERVAL %s DAY
                  AND (
                        (post_type = 'video'    AND (video_url IS NULL OR video_url = ''))
                     OR (post_type IN ('carousel', 'document') AND (
                            carousel_slides IS NULL OR carousel_slides = '' OR carousel_slides = '[]'
                            OR (carousel_slides NOT LIKE '%%http%%'
                                AND carousel_slides NOT LIKE '%%/assets%%'
                                AND carousel_slides NOT LIKE '%%.png%%'
                                AND carousel_slides NOT LIKE '%%.jpg%%')
                        ))
                  )
                ORDER BY scheduled_time
            """, (within_days,))
            return cursor.fetchall()
    except mysql.connector.Error as err:
        myprint(f"Could not get unposted posts missing assets | Error: {err}")
        return []


# ---------------------------------------------------------------------------
# Avatar training records
# ---------------------------------------------------------------------------









_AVATAR_COLUMNS = """id, training_id, model_ref, trigger_word, status, is_active,
                     gender_presentation, age_band, attributes_confirmed_at,
                     approval_status, approved_at, sample_paths, samples_generated_at,
                     sample_regen_count, created_at, updated_at"""


def _avatar_row_to_dict(row: dict) -> dict:
    """One shape for an avatar row everywhere — the guardrails read approval_status and the
    subject clause reads the declared attributes, so neither may depend on which query ran.
    """
    try:
        samples = json.loads(row.get("sample_paths") or "[]")
    except (TypeError, ValueError):
        samples = []
    return {
        "id": row["id"],
        "training_id": row["training_id"],
        "model_ref": row["model_ref"],
        "trigger_word": row["trigger_word"],
        "status": row["status"],
        "is_active": bool(row.get("is_active")),
        "gender_presentation": row.get("gender_presentation"),
        "age_band": row.get("age_band"),
        "attributes_confirmed_at": (row["attributes_confirmed_at"].isoformat()
                                    if row.get("attributes_confirmed_at") else None),
        "approval_status": row.get("approval_status") or AVATAR_APPROVAL_PENDING,
        "approved_at": row["approved_at"].isoformat() if row.get("approved_at") else None,
        "sample_paths": samples if isinstance(samples, list) else [],
        "samples_generated_at": (row["samples_generated_at"].isoformat()
                                 if row.get("samples_generated_at") else None),
        "sample_regen_count": int(row.get("sample_regen_count") or 0),
        "created_at": row["created_at"].isoformat() if row.get("created_at") else None,
        "updated_at": row["updated_at"].isoformat() if row.get("updated_at") else None,
    }


def get_avatar_trainings(user_id: int) -> list[dict]:
    """Every avatar this user has trained, newest first, normalised for the API. [] on a read error."""
    try:
        with db_cursor(dictionary=True) as cursor:
            cursor.execute(
                f"""SELECT {_AVATAR_COLUMNS}
                    FROM avatar_trainings
                    WHERE user_id = %s
                    ORDER BY created_at DESC""",
                (user_id,),
            )
            return [_avatar_row_to_dict(r) for r in cursor.fetchall()]
    except mysql.connector.Error as err:
        myprint(f"Could not fetch avatar trainings for user_id {user_id} | Error: {err}")
        return []


def get_avatar_training(user_id: int, avatar_id: int) -> Optional[dict]:
    """One avatar row, scoped to its owner so an id from another account can never be read."""
    try:
        with db_cursor(dictionary=True) as cursor:
            cursor.execute(
                f"""SELECT {_AVATAR_COLUMNS}
                    FROM avatar_trainings
                    WHERE id = %s AND user_id = %s
                    LIMIT 1""",
                (avatar_id, user_id),
            )
            row = cursor.fetchone()
            return _avatar_row_to_dict(row) if row else None
    except mysql.connector.Error as err:
        myprint(f"Could not fetch avatar {avatar_id} for user_id {user_id} | Error: {err}")
        return None


def get_active_avatar(user_id: int) -> Optional[dict]:
    """The account's active avatar row, or None when nothing is active or the read failed."""
    try:
        with db_cursor(dictionary=True) as cursor:
            cursor.execute(
                f"""SELECT {_AVATAR_COLUMNS}
                    FROM avatar_trainings
                    WHERE user_id = %s AND is_active = 1
                    LIMIT 1""",
                (user_id,),
            )
            row = cursor.fetchone()
            return _avatar_row_to_dict(row) if row else None
    except mysql.connector.Error as err:
        myprint(f"Could not fetch active avatar for user_id {user_id} | Error: {err}")
        return None












def get_avatar_preferences(user_id: int) -> dict:
    """Per-user avatar guardrails (issue #744, decision 4A).

    Every flag defaults OFF and an unreadable row returns the defaults, so a DB blip degrades to
    "don't use the avatar" rather than to publishing a synthetic likeness.
    """
    from cqc_lem.utilities.avatar.guardrails import DEFAULT_AVATAR_PREFERENCES
    prefs = dict(DEFAULT_AVATAR_PREFERENCES)
    try:
        with db_cursor(dictionary=True) as cursor:
            cursor.execute(
                """SELECT avatar_disabled, avatar_use_post_image, avatar_use_carousel,
                          avatar_use_video, avatar_use_newsletter
                   FROM users WHERE id = %s""",
                (user_id,),
            )
            row = cursor.fetchone()
            if row:
                prefs = {key: bool(row.get(key)) for key in prefs}
            return prefs
    except mysql.connector.Error as err:
        myprint(f"Could not fetch avatar preferences for user_id {user_id} | Error: {err}")
        return prefs


def update_avatar_preferences(user_id: int, prefs: dict) -> bool:
    """Update only the avatar guardrail flags the caller actually supplied."""
    from cqc_lem.utilities.avatar.guardrails import DEFAULT_AVATAR_PREFERENCES
    updates = {k: bool(v) for k, v in (prefs or {}).items()
               if k in DEFAULT_AVATAR_PREFERENCES and v is not None}
    if not updates:
        return False

    try:
        with db_cursor(commit=True) as cursor:
            assignments = ", ".join(f"{key} = %s" for key in updates)
            cursor.execute(
                f"UPDATE users SET {assignments} WHERE id = %s",
                (*[int(v) for v in updates.values()], user_id),
            )
            return True
    except mysql.connector.Error as err:
        myprint(f"Could not update avatar preferences for user_id {user_id} | Error: {err}")
        return False


def get_post_use_avatar(post_id: Optional[int]) -> Optional[bool]:
    """The compose-time avatar choice for a post — None when the user made no choice."""
    if not post_id:
        return None
    try:
        with db_cursor() as cursor:
            cursor.execute("SELECT use_avatar FROM posts WHERE id = %s", (post_id,))
            row = cursor.fetchone()
            if not row or row[0] is None:
                return None
            return bool(row[0])
    except mysql.connector.Error as err:
        myprint(f"Could not fetch use_avatar for post_id {post_id} | Error: {err}")
        return None


def update_post_use_avatar(post_id: int, use_avatar: Optional[bool]) -> bool:
    """Set the compose-time avatar choice on an existing post. None clears it back to
    "follow my preferences" — the field is three-valued everywhere it is read.
    """
    try:
        with db_cursor(commit=True) as cursor:
            cursor.execute(
                "UPDATE posts SET use_avatar = %s WHERE id = %s",
                (None if use_avatar is None else int(bool(use_avatar)), post_id),
            )
            return cursor.rowcount > 0
    except mysql.connector.Error as err:
        myprint(f"Could not update use_avatar for post_id {post_id} | Error: {err}")
        return False


def mark_post_avatar_media(post_id: Optional[int]) -> bool:
    """Record that generated media for this post came out of the avatar LoRA.

    This is what lets the caption disclosure cover avatar IMAGES and not just video — the
    generation step that knows an avatar was used is far away from the step that writes the
    caption, so the fact has to be durable.
    """
    if not post_id:
        return False
    try:
        with db_cursor(commit=True) as cursor:
            cursor.execute("UPDATE posts SET avatar_media = 1 WHERE id = %s", (post_id,))
            return cursor.rowcount > 0
    except mysql.connector.Error as err:
        myprint(f"Could not mark avatar media on post_id {post_id} | Error: {err}")
        return False


def post_used_avatar_media(post_id: Optional[int]) -> bool:
    """Did any generated media on this post come out of the avatar path (issue #744)?

    What the AI-disclosure line is applied on. Fail-soft in both directions: a falsy post_id and a read
    error both return False, so an unreadable flag costs a disclosure rather than the post.
    """
    if not post_id:
        return False
    try:
        with db_cursor() as cursor:
            cursor.execute("SELECT avatar_media FROM posts WHERE id = %s", (post_id,))
            row = cursor.fetchone()
            return bool(row and row[0])
    except mysql.connector.Error as err:
        myprint(f"Could not read avatar_media for post_id {post_id} | Error: {err}")
        return False


# --- Cost ledger writers (issue #490) ------------------------------------------------------
# Durable, Stripe-joinable spend. PostHog is the fast analytics plane; these rows are the exact
# source of truth the margin report joins against MRR. High-volume LLM spend arrives here already
# rolled up (one row per user x feature x tier x day) — see observability.flush_llm_cost_rollup.






def get_users_proxy_config(user_ids: list) -> list:
    """(user_id, proxy_url, country) for the given users — the inputs proxy.resolve_proxy() needs
    to decide which egress proxy (and therefore which monthly cost) applies to each user.
    """
    if not user_ids:
        return []

    try:
        with db_cursor(dictionary=True) as cursor:
            placeholders = ", ".join(["%s"] * len(user_ids))
            cursor.execute(
                f"SELECT id, proxy_url, country FROM users WHERE id IN ({placeholders})",
                tuple(int(uid) for uid in user_ids),
            )
            return [
                {"user_id": row["id"], "proxy_url": row.get("proxy_url"), "country": row.get("country")}
                for row in (cursor.fetchall() or [])
            ]
    except mysql.connector.Error as err:
        myprint(f"Could not fetch proxy config for users | Error: {err}")
        return []


# Cost/margin reporting (docs/cost-performance-margin-plan.md §A.3/§C.1). These are READ-ONLY over
# the `cost_ledger` table the writers above fill. Every one degrades to an empty result when the
# table isn't present yet, so the margin report ships ahead of it.



def cost_ledger_available() -> bool:
    """True when the durable cost_ledger table exists. The margin report uses this to say whether a
    $0 spend figure means "nothing spent" or "not capturing yet".
    """
    try:
        with db_cursor() as cursor:
            cursor.execute("SHOW TABLES LIKE 'cost_ledger'")
            return cursor.fetchone() is not None
    except mysql.connector.Error:
        return False




def get_user_cost(user_id: int, start_date, end_date) -> dict:
    """One user's spend over a window, grouped by cost category (llm/media/proxy/infra/...)."""
    return get_cost_rollup(start_date, end_date, group_by="category", user_id=user_id)




def get_post_quality_rows(start_date, end_date) -> list:
    """Per-post QUALITY observations across all users over [start_date, end_date] — the outcome side
    of the cost-aware routing experiment (docs/cost-performance-margin-plan.md §D.1(1), issue #494):
    `{user_id, post_id, day, reactions, comments, reposts, impressions, authenticity_score}` for
    every POSTED post with captured stats, using the LATEST `post_stats` row per post.

    Read-only and cross-user by design — the A/B arms are cohorts of users, so the comparison has to
    see every user's posts, unlike the per-user `get_post_engagement_rows`.
    """
    try:
        with db_cursor(dictionary=True) as cursor:
            cursor.execute(
                "SELECT p.user_id, p.id AS post_id, DATE(p.scheduled_time) AS day, "
                "p.authenticity_score, s.reactions, s.comments, s.reposts, s.impressions "
                "FROM posts p JOIN post_stats s ON s.post_id=p.id AND s.user_id=p.user_id "
                "WHERE p.status='posted' AND p.scheduled_time BETWEEN %s AND %s "
                "AND s.id IN (SELECT MAX(id) FROM post_stats GROUP BY post_id)",
                (start_date, end_date))
            rows = cursor.fetchall() or []
            return [
                {
                    "user_id": r["user_id"],
                    "post_id": r["post_id"],
                    "day": r["day"].isoformat() if hasattr(r.get("day"), "isoformat") else r.get("day"),
                    "reactions": int(r["reactions"] or 0),
                    "comments": int(r["comments"] or 0),
                    "reposts": int(r["reposts"] or 0),
                    "impressions": int(r["impressions"]) if r.get("impressions") else None,
                    "authenticity_score": (int(r["authenticity_score"])
                                           if r.get("authenticity_score") is not None else None),
                }
                for r in rows
            ]
    except mysql.connector.Error as err:
        myprint(f"Could not get post quality rows | Error: {err}")
        return []


def get_margin_users() -> list:
    """Users the margin report covers: everyone on an active/past-due subscription or an open trial.
    Trials are included (tier `free_trial`, $0 MRR) so the cost they incur still lands in system
    margin instead of vanishing. `cohort` is the signup month — `users` has no created_at, so
    trial_started_at is the signup timestamp, falling back to updated_at for pre-trial rows.
    """
    try:
        with db_cursor(dictionary=True) as cursor:
            cursor.execute(
                """SELECT id, subscription_tier, subscription_status,
                          DATE_FORMAT(COALESCE(trial_started_at, updated_at), '%Y-%m') AS cohort
                   FROM users
                   WHERE subscription_status IN ('active', 'past_due', 'trial')"""
            )
            return cursor.fetchall() or []
    except mysql.connector.Error as err:
        myprint(f"Could not fetch margin users | Error: {err}")
        return []








def _prefixed_feedback_columns(alias: str = "f") -> str:
    return ", ".join(f"{alias}.{c.strip()}" for c in _FEEDBACK_COLUMNS.split(","))


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








def admin_email_allowlist() -> set:
    """Emails from ADMIN_USER_EMAILS, lowercased (issue #793). Empty adds nobody."""
    from cqc_lem.utilities.env_constants import ADMIN_USER_EMAILS
    return {e.strip().lower() for e in (ADMIN_USER_EMAILS or "").split(",") if e.strip()}


def is_user_admin(user_id: int) -> bool:
    """Whether this user is designated as an admin (issue #793).

    Admin is the users.is_admin column OR a match in the ADMIN_USER_EMAILS allowlist — the latter
    exists so a deploy with no flagged user yet can still reach the triage panel and release the
    feedback the auto-filer is now parking.

    Fails CLOSED — a missing user or DB error is never interpreted as admin rights.
    """
    if user_id is None:
        return False
    try:
        with db_cursor(dictionary=True) as cursor:
            cursor.execute("SELECT is_admin, email FROM users WHERE id = %s", (int(user_id),))
            row = cursor.fetchone()
            if not row:
                return False
            if row.get("is_admin"):
                return True
            return (row.get("email") or "").strip().lower() in admin_email_allowlist()
    except mysql.connector.Error as err:
        log_error(f"Could not check admin status for user_id {user_id}", exc=err)
        return False


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












def get_survey_candidate_user_ids() -> list:
    """Users worth surveying: on an active plan or an unexpired trial. Unlike the onboarding
    candidates this does NOT exclude activated users — activation is exactly what makes someone
    worth asking (the day-3 NPS fires off their activation timestamp).
    """
    try:
        with db_cursor() as cursor:
            cursor.execute("""
                SELECT id FROM users
                WHERE subscription_status = 'active'
                   OR (subscription_status = 'trial'
                       AND (trial_ends_at IS NULL OR trial_ends_at > NOW()))
            """)
            return [row[0] for row in cursor.fetchall()]
    except mysql.connector.Error as err:
        log_error("Could not get survey candidate user ids", exc=err)
        return []






































# --- Onboarding / activation checklist (issue #500) ---------------------------------
def ensure_onboarding_state(user_id: int) -> bool:
    """Create the user's onboarding row if it doesn't exist. `started_at` is the trial start when we
    know it, so the nudge clock measures time-since-signup rather than time-since-first-scan.
    """
    try:
        with db_cursor(commit=True) as cursor:
            cursor.execute(
                "INSERT IGNORE INTO onboarding_state (user_id, started_at) "
                "SELECT id, COALESCE(trial_started_at, NOW()) FROM users WHERE id = %s", (user_id,))
            return True
    except mysql.connector.Error as err:
        log_error(f"Could not ensure onboarding state for user_id {user_id}", exc=err)
        return False


def get_onboarding_state(user_id: int) -> dict:
    """The persisted checklist row (started_at + one completion timestamp per step). Empty dict when
    the user has no row yet.
    """
    try:
        with db_cursor(dictionary=True) as cursor:
            cursor.execute(
                f"SELECT user_id, started_at, {', '.join(_ONBOARDING_COLS)} "
                f"FROM onboarding_state WHERE user_id = %s", (user_id,))
            return cursor.fetchone() or {}
    except mysql.connector.Error as err:
        log_error(f"Could not get onboarding state for user_id {user_id}", exc=err)
        return {}


def mark_onboarding_step(user_id: int, step: "OnboardingStep") -> bool:
    """Stamp a checklist step as complete. Idempotent: only the FIRST completion writes, and True is
    returned only then — so the caller emits its PostHog event exactly once.
    """
    column = f"{OnboardingStep(step).value}_at"
    try:
        with db_cursor(commit=True) as cursor:
            cursor.execute(
                f"UPDATE onboarding_state SET {column} = NOW() "
                f"WHERE user_id = %s AND {column} IS NULL", (user_id,))
            return cursor.rowcount > 0
    except mysql.connector.Error as err:
        log_error(f"Could not mark onboarding step {step} for user_id {user_id}", exc=err)
        return False


def get_onboarding_nudges_sent(user_id: int) -> dict:
    """nudge_key -> sent_at for every nudge already delivered to this user."""
    try:
        with db_cursor() as cursor:
            cursor.execute("SELECT nudge_key, sent_at FROM onboarding_nudges WHERE user_id = %s",
                           (user_id,))
            return {row[0]: row[1] for row in cursor.fetchall()}
    except mysql.connector.Error as err:
        log_error(f"Could not get onboarding nudges for user_id {user_id}", exc=err)
        return {}


def record_onboarding_nudge(user_id: int, nudge_key: str) -> bool:
    """Record that a nudge was sent. Returns False when this nudge was already sent (the PK makes
    each nudge one-shot per user).
    """
    try:
        with db_cursor(commit=True) as cursor:
            cursor.execute("INSERT IGNORE INTO onboarding_nudges (user_id, nudge_key) VALUES (%s, %s)",
                           (user_id, str(nudge_key)[:32]))
            return cursor.rowcount > 0
    except mysql.connector.Error as err:
        log_error(f"Could not record onboarding nudge {nudge_key} for user_id {user_id}", exc=err)
        return False


def get_onboarding_candidate_user_ids() -> list:
    """Users still working toward activation: paying or on an unexpired trial, and not yet activated.
    Deliberately NOT get_active_user_ids() — that requires a live LinkedIn connection, which is the
    very step most stalled users are stuck on.
    """
    try:
        with db_cursor() as cursor:
            cursor.execute("""
                SELECT u.id
                FROM users u
                LEFT JOIN onboarding_state o ON o.user_id = u.id
                WHERE (
                        u.subscription_status = 'active'
                        OR (u.subscription_status = 'trial'
                            AND (u.trial_ends_at IS NULL OR u.trial_ends_at > NOW()))
                      )
                  AND o.activated_at IS NULL
            """)
            return [row[0] for row in cursor.fetchall()]
    except mysql.connector.Error as err:
        log_error("Could not get onboarding candidate user ids", exc=err)
        return []


def has_engagement_preferences(user_id: int) -> bool:
    """True when the user has actually SAVED engagement preferences. get_engagement_preferences()
    returns code defaults for everyone, so only the row's existence proves they configured it.

    The two-valued view of `engagement_preferences_are_configured` for callers that only steer UI
    copy: an unreadable row reads as False, exactly as this has always behaved. A caller that would
    WRITE on the answer must use the three-valued function instead (issue #952).
    """
    return engagement_preferences_are_configured(user_id) is True


def has_post_with_status(user_id: int, statuses: tuple) -> bool:
    """True when the user has at least one post in any of the given statuses."""
    if not statuses:
        return False
    try:
        with db_cursor() as cursor:
            placeholders = ", ".join(["%s"] * len(statuses))
            cursor.execute(
                f"SELECT 1 FROM posts WHERE user_id = %s AND status IN ({placeholders}) LIMIT 1",
                (user_id, *[str(s) for s in statuses]))
            return cursor.fetchone() is not None
    except mysql.connector.Error as err:
        log_error(f"Could not check posts for user_id {user_id}", exc=err)
        return False




# ---------------------------------------------------------------------------
# Early-adopter extended trial (issue #499)
# ---------------------------------------------------------------------------

# Cohorts are tried in order: P0 (the hand-picked launch group) fills first, then P1. Capacities
# come from env at call time so the caps can be retuned without a migration or a code change.
EARLY_ADOPTER_COHORTS = ("P0", "P1")

# Statuses an extension may act on. A paying ('active'/'past_due') or churned ('cancelled') user is
# not on a trial, so extending one would either be a no-op or silently reopen a closed account.
# The subscription statuses for which `users.trial_ends_at` is a live date rather than a leftover:
# a paid or cancelled account carries an old value that must never be extended or quoted back.
TRIAL_EXTENDABLE_STATUSES = ("trial", "inactive")


def _as_naive_utc(dt: Optional[datetime]) -> Optional[datetime]:
    """MySQL DATETIME columns come back naive; our own timestamps are UTC-aware. Normalize both to
    naive-UTC so they can be compared without a TypeError.
    """
    if dt is None or dt.tzinfo is None:
        return dt
    return dt.astimezone(timezone.utc).replace(tzinfo=None)








def extend_trial_for_user(user_id: int, feedback_id: Optional[int] = None) -> dict:
    """Claim an early-adopter cohort slot and extend the user's trial to
    `trial_started_at + EARLY_ADOPTER_TRIAL_DAYS` (issue #499).

    The caller owns the review gate; this owns atomicity. Everything below runs in ONE transaction:
    the slot claim is a single conditional UPDATE (its rowcount IS the claim result, so two
    concurrent requests can never both take the last slot), and the unique `user_id` on
    early_adopter_grants means a duplicate request rolls the whole thing back — including the
    counter — rather than burning a second slot.

    Returns a dict the API can hand straight to the SPA:
      granted, reason, cohort, trial_days, trial_ends_at
    where reason is one of granted | already_granted | slots_exhausted | not_on_trial |
    user_not_found | error.
    """
    from cqc_lem.utilities.env_constants import (
        EARLY_ADOPTER_P0_SLOTS,
        EARLY_ADOPTER_P1_SLOTS,
        EARLY_ADOPTER_TRIAL_DAYS,
        FREE_TRIAL_DAYS,
    )
    capacities = {"P0": EARLY_ADOPTER_P0_SLOTS, "P1": EARLY_ADOPTER_P1_SLOTS}

    def _result(granted: bool, reason: str, cohort: Optional[str] = None,
                trial_days: int = FREE_TRIAL_DAYS, trial_ends_at: Optional[datetime] = None) -> dict:
        return {"granted": granted, "reason": reason, "cohort": cohort,
                "trial_days": trial_days, "trial_ends_at": trial_ends_at}

    connection = _connection.get_db_connection()
    cursor = connection.cursor(dictionary=True)
    try:
        connection.start_transaction()

        cursor.execute(
            "SELECT cohort, trial_days, trial_ends_at FROM early_adopter_grants WHERE user_id=%s FOR UPDATE",
            (user_id,),
        )
        existing = cursor.fetchone()
        if existing:
            connection.rollback()
            return _result(True, "already_granted", existing["cohort"],
                           int(existing["trial_days"]), existing["trial_ends_at"])

        cursor.execute(
            "SELECT subscription_status, trial_started_at, trial_ends_at FROM users WHERE id=%s FOR UPDATE",
            (user_id,),
        )
        user = cursor.fetchone()
        if not user:
            connection.rollback()
            return _result(False, "user_not_found")
        if user["subscription_status"] not in TRIAL_EXTENDABLE_STATUSES:
            connection.rollback()
            return _result(False, "not_on_trial")

        claimed: Optional[str] = None
        for cohort in EARLY_ADOPTER_COHORTS:
            capacity = int(capacities.get(cohort, 0))
            if capacity <= 0:
                continue
            cursor.execute(
                "UPDATE early_adopter_slots SET used = used + 1 WHERE cohort=%s AND used < %s",
                (cohort, capacity),
            )
            if cursor.rowcount == 1:
                claimed = cohort
                break
        if not claimed:
            connection.rollback()
            return _result(False, "slots_exhausted")

        started = _as_naive_utc(user["trial_started_at"]) or datetime.now(timezone.utc).replace(tzinfo=None)
        new_end = started + timedelta(days=EARLY_ADOPTER_TRIAL_DAYS)
        current_end = _as_naive_utc(user["trial_ends_at"])
        # An extension must never shorten a trial the user already has.
        if current_end and current_end > new_end:
            new_end = current_end

        cursor.execute(
            "INSERT INTO early_adopter_grants (user_id, cohort, trial_days, feedback_id, trial_ends_at) "
            "VALUES (%s,%s,%s,%s,%s)",
            (user_id, claimed, EARLY_ADOPTER_TRIAL_DAYS, feedback_id, new_end),
        )
        cursor.execute(
            "UPDATE users SET trial_started_at=%s, trial_ends_at=%s, subscription_status='trial', "
            "subscription_tier=COALESCE(subscription_tier,'free_trial') WHERE id=%s",
            (started, new_end, user_id),
        )
        connection.commit()
        log_info("Early-adopter trial granted", user_id=user_id)
        return _result(True, "granted", claimed, EARLY_ADOPTER_TRIAL_DAYS, new_end)
    except mysql.connector.Error as err:
        connection.rollback()
        if err.errno == errorcode.ER_DUP_ENTRY:
            # Two concurrent requests for the same user; the rollback released the slot this one took.
            existing = get_early_adopter_grant(user_id)
            if existing:
                return _result(True, "already_granted", existing["cohort"],
                               int(existing["trial_days"]), existing["trial_ends_at"])
        log_error("Could not extend trial", exc=err, user_id=user_id)
        return _result(False, "error")
    finally:
        cursor.close()
        connection.close()


# --- Affiliate / ambassador program (issue #737) ---------------------------------------------------
# Trial days are the reward currency, so every write that moves `users.trial_ends_at` runs inside ONE
# transaction with its ledger row: a grant that lands without its ledger entry is free service nobody
# can account for, and a ledger row without the extension is a promise we broke.

def _affiliate_row(row: Optional[dict]) -> Optional[dict]:
    """Normalize an enrollment row for callers: booleans as booleans, status as a plain string."""
    if not row:
        return None
    return {
        "user_id": int(row["user_id"]),
        "status": str(row["status"]),
        "referral_code": str(row.get("referral_code") or ""),
        "enrolled_at": row.get("enrolled_at"),
        "opted_out_at": row.get("opted_out_at"),
        "notice_seen_at": row.get("notice_seen_at"),
        "promo_content_opt_in": bool(row.get("promo_content_opt_in")),
        "promo_consent_at": row.get("promo_consent_at"),
        "promo_consent_version": row.get("promo_consent_version"),
    }


def get_affiliate_enrollment(user_id: int) -> Optional[dict]:
    """The user's affiliate row, or None when they have never been enrolled.

    The row here carries columns only — no `created` key. That flag exists solely on what
    `ensure_affiliate_enrollment` returns, because only the call that wrote the row can know it.
    """
    try:
        with db_cursor(dictionary=True) as cursor:
            cursor.execute(
                "SELECT user_id, status, referral_code, enrolled_at, opted_out_at, notice_seen_at, "
                "promo_content_opt_in, promo_consent_at, promo_consent_version "
                "FROM affiliate_enrollments WHERE user_id=%s",
                (user_id,),
            )
            return _affiliate_row(cursor.fetchone())
    except mysql.connector.Error as err:
        log_error("Could not read affiliate enrollment", exc=err, user_id=user_id)
        return None


def ensure_affiliate_enrollment(user_id: int, status: str = 'enrolled',
                                referral_code: Optional[str] = None) -> Optional[dict]:
    """Create the user's affiliate row if it doesn't exist, then return it.

    Idempotent by INSERT IGNORE rather than read-then-write: two requests racing on a first page
    load must not produce two rows or a duplicate-key 500. An existing row is never re-statused
    here — an opted-out user staying opted out is the entire point of the opt-out.

    The returned row carries `created` — whether THIS call is the one that enrolled them. Every
    Account page load calls this, so it is the only way the caller can emit an enrollment event once
    instead of on every render. It is a synthetic key, not a column: `get_affiliate_enrollment`
    never sets it, and nothing that serializes the row to a client reads it (`affiliate_state`
    builds its payload field by field).

    On a DB error this returns None, so a caller can never read `created=False` from a write that
    did not happen — the row will not exist either, and the next call re-inserts it.
    """
    code = str(referral_code or user_id)
    connection = _connection.get_db_connection()
    cursor = connection.cursor()
    created = False
    try:
        cursor.execute(
            "INSERT IGNORE INTO affiliate_enrollments (user_id, status, referral_code, enrolled_at) "
            "VALUES (%s,%s,%s,%s)",
            (user_id, str(status), code,
             datetime.now(timezone.utc) if str(status) == str(AffiliateStatus.ENROLLED) else None),
        )
        created = cursor.rowcount == 1
        connection.commit()
    except mysql.connector.Error as err:
        log_error("Could not create affiliate enrollment", exc=err, user_id=user_id)
        return None
    finally:
        cursor.close()
        connection.close()
    row = get_affiliate_enrollment(user_id)
    if row is not None:
        row["created"] = created
    return row


def set_affiliate_status(user_id: int, enrolled: bool) -> Optional[dict]:
    """Opt the user in or out of (A) affiliate status. Immediate — the caller reflects the resulting
    trial length back to the user, and the reward side (grant/revoke of the enrollment bonus) is the
    caller's separate, ledgered step so a status flip can never silently move money.
    """
    status = AffiliateStatus.ENROLLED if enrolled else AffiliateStatus.OPTED_OUT
    now = datetime.now(timezone.utc)
    try:
        with db_cursor(commit=True) as cursor:
            cursor.execute(
                "UPDATE affiliate_enrollments SET status=%s, "
                "enrolled_at=IF(%s, COALESCE(enrolled_at,%s), enrolled_at), "
                "opted_out_at=IF(%s, opted_out_at, %s) WHERE user_id=%s",
                (str(status), enrolled, now, enrolled, now, user_id),
            )
    except mysql.connector.Error as err:
        log_error("Could not update affiliate status", exc=err, user_id=user_id)
        return None
    return get_affiliate_enrollment(user_id)


def set_affiliate_promo_opt_in(user_id: int, enabled: bool, consent_version: str) -> Optional[dict]:
    """(B) — whether LEM may publish promotional content about LEM from the user's own LinkedIn
    account. Enabling stamps the consent timestamp AND the version of the copy consented to;
    disabling clears both, so a re-enable can never inherit an old consent record.
    """
    now = datetime.now(timezone.utc) if enabled else None
    try:
        with db_cursor(commit=True) as cursor:
            cursor.execute(
                "UPDATE affiliate_enrollments SET promo_content_opt_in=%s, promo_consent_at=%s, "
                "promo_consent_version=%s WHERE user_id=%s",
                (1 if enabled else 0, now, str(consent_version) if enabled else None, user_id),
            )
    except mysql.connector.Error as err:
        log_error("Could not update affiliate promo consent", exc=err, user_id=user_id)
        return None
    return get_affiliate_enrollment(user_id)














def _affiliate_baseline_trial_end(cursor, user_id: int, started: datetime) -> datetime:
    """The trial end a revoked enrollment bonus may never take the user below: their standard trial,
    any early-adopter grant (#499), and every referral day they EARNED. Only the enrollment bonus is
    contingent on status; nothing else the user holds is.
    """
    from cqc_lem.utilities.env_constants import FREE_TRIAL_DAYS
    baseline = started + timedelta(days=FREE_TRIAL_DAYS)
    cursor.execute("SELECT trial_ends_at FROM early_adopter_grants WHERE user_id=%s", (user_id,))
    grant = cursor.fetchone()
    grant_end = _as_naive_utc(grant["trial_ends_at"]) if grant else None
    if grant_end and grant_end > baseline:
        baseline = grant_end
    cursor.execute(
        "SELECT COALESCE(SUM(trial_days),0) AS days FROM affiliate_rewards WHERE user_id=%s AND kind=%s",
        (user_id, str(AffiliateRewardKind.REFERRAL)),
    )
    earned = cursor.fetchone()
    return baseline + timedelta(days=max(0, int((earned or {}).get("days") or 0)))


def grant_affiliate_trial_days(user_id: int, days: int, kind: str,
                               referral_id: Optional[int] = None,
                               reason: Optional[str] = None) -> dict:
    """Extend the user's trial by `days` and write the matching ledger row, in ONE transaction.

    Capped twice: by `AFFILIATE_MAX_REWARD_DAYS` against the user's own ledger sum (a partial grant
    is granted, not refused — the user gets what is left under the ceiling), and by the ENUM'd
    `kind`. Only trialling users are extended; a paying subscriber has no trial to lengthen, and
    silently paying them in a currency they can't spend would look like a granted reward in the UI.

    Returns `{granted, reason, days, total_days, trial_ends_at}` where reason is one of
    granted | already_granted | capped | not_on_trial | user_not_found | disabled | error.
    """
    from cqc_lem.utilities.marketing.affiliate import grantable_days, program_enabled

    def _result(granted: bool, why: str, days_granted: int = 0, total: int = 0,
                ends_at: Optional[datetime] = None) -> dict:
        return {"granted": granted, "reason": why, "days": days_granted,
                "total_days": total, "trial_ends_at": ends_at}

    if not program_enabled():
        return _result(False, "disabled")
    if int(days) <= 0:
        return _result(False, "capped")

    connection = _connection.get_db_connection()
    cursor = connection.cursor(dictionary=True)
    try:
        connection.start_transaction()
        cursor.execute(
            "SELECT COALESCE(SUM(trial_days),0) AS total FROM affiliate_rewards WHERE user_id=%s FOR UPDATE",
            (user_id,),
        )
        already = int((cursor.fetchone() or {}).get("total") or 0)

        # One enrollment bonus at a time: the migration's UNIQUE key only constrains referral rows
        # (repeated NULLs are legal), so the "already granted" check for the status bonus is held
        # here, inside the same transaction that would pay it.
        if str(kind) == str(AffiliateRewardKind.ENROLLMENT):
            cursor.execute(
                "SELECT COALESCE(SUM(trial_days),0) AS net FROM affiliate_rewards "
                "WHERE user_id=%s AND kind IN (%s,%s) FOR UPDATE",
                (user_id, str(AffiliateRewardKind.ENROLLMENT), str(AffiliateRewardKind.REVOKED)),
            )
            if int((cursor.fetchone() or {}).get("net") or 0) > 0:
                connection.rollback()
                return _result(True, "already_granted", 0, already)

        payable = grantable_days(already, int(days))
        if payable <= 0:
            connection.rollback()
            return _result(False, "capped", 0, already)

        cursor.execute(
            "SELECT subscription_status, trial_started_at, trial_ends_at FROM users WHERE id=%s FOR UPDATE",
            (user_id,),
        )
        user = cursor.fetchone()
        if not user:
            connection.rollback()
            return _result(False, "user_not_found")
        if user["subscription_status"] not in TRIAL_EXTENDABLE_STATUSES:
            connection.rollback()
            return _result(False, "not_on_trial", 0, already)

        now = datetime.now(timezone.utc).replace(tzinfo=None)
        started = _as_naive_utc(user["trial_started_at"]) or now
        # Extend from whichever is later: a trial that already lapsed is extended from TODAY, or the
        # reward would land entirely in the past and read as nothing happening.
        current_end = _as_naive_utc(user["trial_ends_at"]) or now
        new_end = max(current_end, now) + timedelta(days=payable)

        cursor.execute(
            "INSERT INTO affiliate_rewards (user_id, referral_id, kind, trial_days, reason, trial_ends_at) "
            "VALUES (%s,%s,%s,%s,%s,%s)",
            (user_id, referral_id, str(kind), payable, reason, new_end),
        )
        cursor.execute(
            "UPDATE users SET trial_started_at=%s, trial_ends_at=%s, subscription_status='trial', "
            "subscription_tier=COALESCE(subscription_tier,'free_trial') WHERE id=%s",
            (started, new_end, user_id),
        )
        connection.commit()
        log_info(f"Affiliate reward granted: +{payable} trial days ({kind})", user_id=user_id)
        return _result(True, "granted", payable, already + payable, new_end)
    except mysql.connector.Error as err:
        connection.rollback()
        if err.errno == errorcode.ER_DUP_ENTRY:
            # A concurrent activation already paid this referral. Not an error — the invariant held.
            return _result(True, "already_granted")
        log_error("Could not grant affiliate trial days", exc=err, user_id=user_id)
        return _result(False, "error")
    finally:
        cursor.close()
        connection.close()


def revoke_affiliate_enrollment_bonus(user_id: int) -> dict:
    """Return an opted-out user to their standard trial: subtract the enrollment bonus still standing
    and write the negative ledger row, in one transaction.

    Never takes the trial below `_affiliate_baseline_trial_end` — the standard trial, any
    early-adopter grant, and every referral day the user EARNED all survive an opt-out. That is what
    keeps "your trial returns to the standard N days" true rather than punitive.
    """
    def _result(revoked: bool, why: str, days: int = 0,
                ends_at: Optional[datetime] = None) -> dict:
        return {"revoked": revoked, "reason": why, "days": days, "trial_ends_at": ends_at}

    connection = _connection.get_db_connection()
    cursor = connection.cursor(dictionary=True)
    try:
        connection.start_transaction()
        cursor.execute(
            "SELECT COALESCE(SUM(trial_days),0) AS net FROM affiliate_rewards "
            "WHERE user_id=%s AND kind IN (%s,%s) FOR UPDATE",
            (user_id, str(AffiliateRewardKind.ENROLLMENT), str(AffiliateRewardKind.REVOKED)),
        )
        standing = int((cursor.fetchone() or {}).get("net") or 0)
        if standing <= 0:
            connection.rollback()
            return _result(False, "nothing_to_revoke")

        cursor.execute(
            "SELECT trial_started_at, trial_ends_at FROM users WHERE id=%s FOR UPDATE",
            (user_id,),
        )
        user = cursor.fetchone()
        if not user:
            connection.rollback()
            return _result(False, "user_not_found")

        now = datetime.now(timezone.utc).replace(tzinfo=None)
        started = _as_naive_utc(user["trial_started_at"]) or now
        current_end = _as_naive_utc(user["trial_ends_at"]) or now
        baseline = _affiliate_baseline_trial_end(cursor, user_id, started)
        new_end = max(current_end - timedelta(days=standing), baseline)

        cursor.execute(
            "INSERT INTO affiliate_rewards (user_id, referral_id, kind, trial_days, reason, trial_ends_at) "
            "VALUES (%s,%s,%s,%s,%s,%s)",
            (user_id, None, str(AffiliateRewardKind.REVOKED), -standing, "opted_out", new_end),
        )
        cursor.execute("UPDATE users SET trial_ends_at=%s WHERE id=%s", (new_end, user_id))
        connection.commit()
        log_info(f"Affiliate enrollment bonus revoked: -{standing} trial days", user_id=user_id)
        return _result(True, "revoked", standing, new_end)
    except mysql.connector.Error as err:
        connection.rollback()
        log_error("Could not revoke affiliate enrollment bonus", exc=err, user_id=user_id)
        return _result(False, "error")
    finally:
        cursor.close()
        connection.close()


# --- app-level credentials (issue #742) -------------------------------------------------------
# Named secrets that belong to the INSTALL, not to a user (the YouTube OAuth refresh token today).
# Reads fall back to the env seed at the call site, so an empty table behaves exactly like the
# pre-#742 env-only world; a write here is what lets a re-minted token land without a deploy.





