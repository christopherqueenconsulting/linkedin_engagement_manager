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
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional, Union

import mysql.connector
from dotenv import load_dotenv
from mysql.connector import errorcode

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
    GroupPostMediaType,
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
    get_recent_newsletter_bodies,
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
from cqc_lem.platform.db.repositories.posts import (
    _ALLOWED_POST_CLAUSES,
    READY_POST_STATUSES,
    bulk_update_posts,
    count_ready_posts_within_buffer,
    get_carousel_slides,
    get_content_mix_counts,
    get_content_quality_scores,
    get_dashboard_counts,
    get_engager_candidates,
    get_follower_stats,
    get_last_planned_post_date_for_user,
    get_latest_post_stats,
    get_next_planned_post_date,
    get_next_planned_posts_after_buffer,
    get_orphaned_scheduled_posts,
    get_planned_posts_within_buffer,
    get_post_archetype,
    get_post_authenticity_score,
    get_post_buyer_stage,
    get_post_captions,
    get_post_carousel_slides,
    get_post_content,
    get_post_content_mix,
    get_post_coverage_counts,
    get_post_dwell_score,
    get_post_engagement_rows,
    get_post_first_comment_link,
    get_post_gate_reason,
    get_post_image_url,
    get_post_manual_publish,
    get_post_performance_rows,
    get_post_quality_rows,
    get_post_rejection_reason,
    get_post_status,
    get_post_type,
    get_post_type_counts,
    get_post_use_avatar,
    get_post_user_id,
    get_post_video_quality,
    get_post_video_url,
    get_posted_posts,
    get_ready_to_post_posts,
    get_recent_engagers,
    get_recent_post_shape_history,
    get_recent_post_texts,
    get_recent_posted_post_ids,
    get_shape_performance,
    get_shipped_content_for_quality,
    get_shipped_variant_keys,
    get_uncaptured_posted_post_ids,
    get_unposted_posts_missing_assets,
    get_user_ids_with_planned_posts_within_buffer,
    get_variant_outcome_rows,
    has_post_with_status,
    has_scheduled_post_today,
    insert_occasion_post,
    insert_planned_post,
    mark_post_avatar_media,
    post_avatar_media_state,
    post_used_avatar_media,
    record_content_quality_score,
    record_follower_stat,
    record_post_stats,
    record_shipped_variant,
    replace_video_url_base,
    update_db_post,
    update_db_post_authenticity_score,
    update_db_post_captions,
    update_db_post_carousel_slides,
    update_db_post_content,
    update_db_post_dwell_score,
    update_db_post_first_comment_link,
    update_db_post_gate_reason,
    update_db_post_image_url,
    update_db_post_rejection_reason,
    update_db_post_shape,
    update_db_post_status,
    update_db_post_video_url,
    update_post_use_avatar,
    update_post_video_quality,
    upsert_engager,
    user_owns_posts,
)
from cqc_lem.platform.db.repositories.users import (
    _ALLOWED_USER_CLAUSES,
    _ONBOARDING_COLS,
    _SECRET_UPDATE_SQL,
    MAX_CONTENT_BUFFER_POSTS,
    SECRET_FIELD_ACCESS_TOKEN,
    SECRET_FIELD_COOKIE_VALUE,
    SECRET_FIELD_PASSWORD,
    SECRET_FIELD_REFRESH_TOKEN,
    _store_cookie_rows,
    add_linkedin_profile,
    add_user,
    add_user_by_email,
    add_user_with_access_token,
    change_user_email,
    clear_user_linkedin_password,
    encrypt_secrets_at_rest,
    engagement_preferences_are_configured,
    ensure_onboarding_state,
    get_active_user_ids,
    get_avatar_preferences,
    get_company_linked_in_url_for_user,
    get_cookies,
    get_default_video_quality,
    get_last_recorded_skills,
    get_linked_in_profile_by_email,
    get_linked_in_profile_by_url,
    get_linked_in_profile_by_user_id,
    get_linkedin_profile_url_by_user_id,
    get_linkedin_session_email_sent_at,
    get_linkedin_token_user_ids,
    get_margin_users,
    get_onboarding_candidate_user_ids,
    get_onboarding_nudges_sent,
    get_onboarding_state,
    get_or_create_reply_inbound_token,
    get_profile_synthesis,
    get_survey_candidate_user_ids,
    get_user_access_token,
    get_user_analytics_profile,
    get_user_blog_url,
    get_user_by_stripe_customer_id,
    get_user_content_language,
    get_user_email,
    get_user_geo,
    get_user_id,
    get_user_id_by_public_uid,
    get_user_id_by_reply_token,
    get_user_ids_needing_profile_synthesis,
    get_user_linked_sub_id,
    get_user_linkedin_display_name,
    get_user_location,
    get_user_password_pair_by_id,
    get_user_preferences,
    get_user_proxy,
    get_user_public_uid,
    get_user_sitemap_url,
    get_user_subscription_info,
    get_user_timezone,
    get_user_token_info,
    get_users_proxy_config,
    get_users_with_reply_mode,
    get_users_with_stripe_subscriptions,
    has_linkedin_password,
    has_linkedin_session,
    mark_email_verified,
    mark_onboarding_step,
    prune_superseded_cookies,
    record_onboarding_nudge,
    remove_linked_in_profile_by_email,
    remove_linked_in_profile_by_url,
    remove_linked_in_profile_by_user_id,
    set_last_recorded_skills,
    set_linkedin_session_email_sent_at,
    set_profile_synthesis,
    update_avatar_preferences,
    update_company_linked_in_url_for_user,
    update_linkedin_connection_status,
    update_subscription_from_stripe,
    update_user,
    update_user_access_token,
    update_user_linkedin_display_name,
    update_user_linkedin_password,
    update_user_linkedin_token,
    update_user_location,
    update_user_preferences,
    update_user_proxy,
    update_user_settings,
    update_user_timezone,
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
    hash_client_ip,
    hash_session_token,
)
from cqc_lem.utilities.env_constants import (
    SESSION_IDLE_HOURS,
)
from cqc_lem.utilities.logger import log_error, log_info, log_warning

# Re-exported for the ~2,400 call sites that still say `from cqc_lem.utilities.db import X`.
# `__all__` is the one declaration ruff (F401) and CodeQL (py/unused-import) both understand —
# a per-line ruff directive is invisible to CodeQL and an lgtm marker is invisible to ruff, so
# either one alone leaves whichever tool is blind free to flag or delete these.
__all__ = [
    "MAX_CONTENT_BUFFER_POSTS",
    "SECRET_FIELD_ACCESS_TOKEN",
    "SECRET_FIELD_COOKIE_VALUE",
    "SECRET_FIELD_PASSWORD",
    "SECRET_FIELD_REFRESH_TOKEN",
    "_ALLOWED_USER_CLAUSES",
    "_ONBOARDING_COLS",
    "_SECRET_UPDATE_SQL",
    "_store_cookie_rows",
    "add_linkedin_profile",
    "add_user",
    "add_user_by_email",
    "add_user_with_access_token",
    "change_user_email",
    "clear_user_linkedin_password",
    "encrypt_secrets_at_rest",
    "engagement_preferences_are_configured",
    "ensure_onboarding_state",
    "get_active_user_ids",
    "get_avatar_preferences",
    "get_company_linked_in_url_for_user",
    "get_cookies",
    "get_default_video_quality",
    "get_last_recorded_skills",
    "get_linked_in_profile_by_email",
    "get_linked_in_profile_by_url",
    "get_linked_in_profile_by_user_id",
    "get_linkedin_profile_url_by_user_id",
    "get_linkedin_session_email_sent_at",
    "get_linkedin_token_user_ids",
    "get_margin_users",
    "get_onboarding_candidate_user_ids",
    "get_onboarding_nudges_sent",
    "get_onboarding_state",
    "get_or_create_reply_inbound_token",
    "get_profile_synthesis",
    "get_survey_candidate_user_ids",
    "get_user_access_token",
    "get_user_analytics_profile",
    "get_user_blog_url",
    "get_user_by_stripe_customer_id",
    "get_user_content_language",
    "get_user_email",
    "get_user_geo",
    "get_user_id",
    "get_user_id_by_public_uid",
    "get_user_id_by_reply_token",
    "get_user_ids_needing_profile_synthesis",
    "get_user_linked_sub_id",
    "get_user_linkedin_display_name",
    "get_user_location",
    "get_user_password_pair_by_id",
    "get_user_preferences",
    "get_user_proxy",
    "get_user_public_uid",
    "get_user_sitemap_url",
    "get_user_subscription_info",
    "get_user_timezone",
    "get_user_token_info",
    "get_users_proxy_config",
    "get_users_with_reply_mode",
    "get_users_with_stripe_subscriptions",
    "has_linkedin_password",
    "has_linkedin_session",
    "mark_email_verified",
    "mark_onboarding_step",
    "prune_superseded_cookies",
    "record_onboarding_nudge",
    "remove_linked_in_profile_by_email",
    "remove_linked_in_profile_by_url",
    "remove_linked_in_profile_by_user_id",
    "set_last_recorded_skills",
    "set_linkedin_session_email_sent_at",
    "set_profile_synthesis",
    "update_avatar_preferences",
    "update_company_linked_in_url_for_user",
    "update_linkedin_connection_status",
    "update_subscription_from_stripe",
    "update_user",
    "update_user_access_token",
    "update_user_linkedin_display_name",
    "update_user_linkedin_password",
    "update_user_linkedin_token",
    "update_user_location",
    "update_user_preferences",
    "update_user_proxy",
    "update_user_settings",
    "update_user_timezone",
    "READY_POST_STATUSES",
    "_ALLOWED_POST_CLAUSES",
    "bulk_update_posts",
    "count_ready_posts_within_buffer",
    "get_carousel_slides",
    "get_content_mix_counts",
    "get_content_quality_scores",
    "get_dashboard_counts",
    "get_engager_candidates",
    "get_follower_stats",
    "get_last_planned_post_date_for_user",
    "get_latest_post_stats",
    "get_next_planned_post_date",
    "get_next_planned_posts_after_buffer",
    "get_orphaned_scheduled_posts",
    "get_planned_posts_within_buffer",
    "get_post_archetype",
    "get_post_authenticity_score",
    "get_post_buyer_stage",
    "get_post_captions",
    "get_post_carousel_slides",
    "get_post_content",
    "get_post_content_mix",
    "get_post_coverage_counts",
    "get_post_dwell_score",
    "get_post_engagement_rows",
    "get_post_first_comment_link",
    "get_post_gate_reason",
    "get_post_image_url",
    "get_post_manual_publish",
    "get_post_performance_rows",
    "get_post_quality_rows",
    "get_post_rejection_reason",
    "get_post_status",
    "get_post_type",
    "get_post_type_counts",
    "get_post_use_avatar",
    "get_post_user_id",
    "get_post_video_quality",
    "get_post_video_url",
    "get_posted_posts",
    "get_ready_to_post_posts",
    "get_recent_engagers",
    "get_recent_post_shape_history",
    "get_recent_post_texts",
    "get_recent_posted_post_ids",
    "get_shape_performance",
    "get_shipped_content_for_quality",
    "get_shipped_variant_keys",
    "get_uncaptured_posted_post_ids",
    "get_unposted_posts_missing_assets",
    "get_user_ids_with_planned_posts_within_buffer",
    "get_variant_outcome_rows",
    "has_post_with_status",
    "has_scheduled_post_today",
    "insert_occasion_post",
    "insert_planned_post",
    "mark_post_avatar_media",
    "post_avatar_media_state",
    "post_used_avatar_media",
    "record_content_quality_score",
    "record_follower_stat",
    "record_post_stats",
    "record_shipped_variant",
    "replace_video_url_base",
    "update_db_post",
    "update_db_post_authenticity_score",
    "update_db_post_captions",
    "update_db_post_carousel_slides",
    "update_db_post_content",
    "update_db_post_dwell_score",
    "update_db_post_first_comment_link",
    "update_db_post_gate_reason",
    "update_db_post_image_url",
    "update_db_post_rejection_reason",
    "update_db_post_shape",
    "update_db_post_status",
    "update_db_post_video_url",
    "update_post_use_avatar",
    "update_post_video_quality",
    "upsert_engager",
    "user_owns_posts",
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
    "get_recent_newsletter_bodies",
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
    "GroupPostMediaType",
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








def store_linkedin_li_at(user_id: int, li_at: str, jsessionid: Optional[str] = None) -> bool:
    """Persist a user-supplied LinkedIn session cookie (li_at, optionally JSESSIONID).

    Lets login_to_linkedin resume an already-trusted session instead of doing a fresh
    password login — which is what triggers LinkedIn's new-device challenge. Reuses the
    standard cookie store so the existing cookie-first login path picks it up.
    """
    email = get_user_email(user_id)
    if not email:
        log_info(f"store_linkedin_li_at: no email for user_id {user_id}")
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
        # Same failure as the no-row branch above, which is already ERROR: the caller drops the
        # user's stored password on a True, so a swallowed write here costs them the session.
        log_error("Could not store LinkedIn session cookie", exc=e, user_id=user_id)
        return False


















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
        # WARNING, not INFO: the post is silently dropped. Once is a bad argument; repeatedly is
        # something systematically composing posts against an account that does not exist.
        log_warning("Cannot insert post — no user for that email")
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
        log_error("Could not insert post", exc=e)

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
        log_error(f"Could not get posts for user id: {user_id}", exc=err)
        posts = []
        total = 0
    finally:
        cursor.close()
        connection.close()

    return posts, total




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
        log_error("Could not get planned tasks", exc=err, user_id=user_id)
        return []
    finally:
        cursor.close()
        connection.close()

    tasks.sort(key=lambda t: t["scheduled_time"])
    return tasks[:limit]




def set_default_video_quality(user_id: int, quality: str) -> bool:
    """Set the user's default video quality preference (upserts the engagement_preferences row).
    Invalid values are coerced to 'standard'.
    """
    if quality not in VALID_VIDEO_QUALITIES:
        quality = "standard"
    return update_engagement_preferences(user_id, {"default_video_quality": quality})




# `get_post_by_email` lived here until issue #914. It turned an ADDRESS into somebody's posts, which
# is exactly the shape `GET /posts/` used to authenticate on; its one caller now resolves the caller
# from the session and calls `get_posts(user_id, …)` directly. Leaving the wrapper behind would keep
# an address-keyed reader one import away from the next endpoint — deleted rather than deprecated.






























def soft_delete_posts(post_ids: list[int], rejection_reason: Optional[str] = None,
                      user_id: Optional[int] = None) -> bool:
    """Reject posts rather than delete them, so the row and its reason survive for the plan to learn from.

    `user_id` scopes the write to one account's rows — see `bulk_update_posts` for what that argument
    guarantees (issue #914).
    """
    return bulk_update_posts(post_ids, status=PostStatus.REJECTED, rejection_reason=rejection_reason,
                             user_id=user_id)








































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


























































def has_user_commented_on_post_url(user_id: int, post_url: str):
    """Have we already left a top-level comment on this post URL?

    Replies do not count (see `count_user_comments_on_post_url`). A failed read counts zero, so an
    unreadable log reads as "not yet" and the post can be commented on again.
    """
    return count_user_comments_on_post_url(user_id, post_url) > 0


































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
        log_error("Could not create session", exc=err, user_id=user_id)
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
        log_error("Could not list sessions", exc=err, user_id=user_id)
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
        log_error("Could not store passkey", exc=err, user_id=user_id)
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
        log_error("Could not look up passkey", exc=err)
        return None
















































# ---------------------------------------------------------------------------
# User helpers
# ---------------------------------------------------------------------------

































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
        log_error("Could not get engagement prefs", exc=err, user_id=user_id)
        return _code_engagement_defaults(user_id)
    return _code_engagement_defaults(user_id) if row is None else row




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
        log_error("Could not update engagement prefs", exc=err, user_id=user_id)
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




















































































def _group_post_draft_row(row: dict) -> dict:
    row = dict(row)
    for col in ("created_at", "updated_at", "published_at"):
        val = row.get(col)
        row[col] = val.isoformat() if hasattr(val, "isoformat") else val
    return row




_GROUP_POST_DRAFT_COLUMNS = ("id, user_id, group_id, group_name, content, media_url, media_type, "
                             "status, created_at, updated_at, published_at")


def get_open_group_post_draft(user_id: int) -> Optional[dict]:
    """The user's ONE open group-post draft, or None when nothing is waiting.

    This is the row the weekly publish run consumes and the one the draft beat checks for before
    writing another.

    READY only: a SKIPPED draft is not open, so skipping this week lets the next beat draft afresh.
    The SPA reads `get_current_group_post_draft` instead, because a user who skipped by accident has
    to be able to see the draft to restore it (issue #1224).
    """
    try:
        with db_cursor(dictionary=True) as cursor:
            cursor.execute(
                f"SELECT {_GROUP_POST_DRAFT_COLUMNS} FROM group_post_drafts "
                "WHERE user_id=%s AND status=%s ORDER BY id DESC LIMIT 1",
                (user_id, str(GroupPostDraftStatus.READY)))
            row = cursor.fetchone()
            return _group_post_draft_row(row) if row else None
    except mysql.connector.Error as err:
        log_error("Could not read the open group post draft", exc=err, user_id=user_id)
        return None


def get_current_group_post_draft(user_id: int) -> Optional[dict]:
    """The group-post draft the Content Studio shows (issue #1224).

    The user's newest draft that is still THEIRS to decide: READY, or SKIPPED and therefore
    restorable.

    An open draft always wins over a skipped one, whatever their ids say, so restoring an old skip
    can never hide the post that is about to ship. PUBLISHED and FAILED rows are history and are
    never returned — there is nothing left to edit on them.
    """
    try:
        with db_cursor(dictionary=True) as cursor:
            cursor.execute(
                f"SELECT {_GROUP_POST_DRAFT_COLUMNS} FROM group_post_drafts "
                "WHERE user_id=%s AND status IN (%s, %s) "
                "ORDER BY status = %s DESC, id DESC LIMIT 1",
                (user_id, str(GroupPostDraftStatus.READY), str(GroupPostDraftStatus.SKIPPED),
                 str(GroupPostDraftStatus.READY)))
            row = cursor.fetchone()
            return _group_post_draft_row(row) if row else None
    except mysql.connector.Error as err:
        log_error("Could not read the current group post draft", exc=err, user_id=user_id)
        return None


def get_group_post_draft(draft_id: int) -> Optional[dict]:
    """One group-post draft by id, normalised for the API, or None when missing or unreadable."""
    try:
        with db_cursor(dictionary=True) as cursor:
            cursor.execute(
                f"SELECT {_GROUP_POST_DRAFT_COLUMNS} FROM group_post_drafts WHERE id=%s",
                (draft_id,))
            row = cursor.fetchone()
            return _group_post_draft_row(row) if row else None
    except mysql.connector.Error as err:
        log_error("Could not read group post draft", exc=err, task_name="get_group_post_draft")
        return None


















































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
                log_error(f"Could not read {kind} lead activity", exc=err, user_id=user_id)
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
        log_error("Could not read profile facts", exc=err)
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
        log_error("Could not count artifact CTA deliveries", exc=err, user_id=user_id)
        return out


















































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
        log_error("Could not count existing double-sent catch-ups", exc=err)
        return 0






























# ---------------------------------------------------------------------------
# Avatar credit ledger
# ---------------------------------------------------------------------------













# ---------------------------------------------------------------------------
# Premium video credits (mirrors avatar_credit_ledger; balance = SUM(delta))
# ---------------------------------------------------------------------------



















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
        log_error("Could not fetch avatar trainings", exc=err, user_id=user_id)
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
        log_error(f"Could not fetch avatar {avatar_id}", exc=err, user_id=user_id)
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
        log_error("Could not fetch active avatar", exc=err, user_id=user_id)
        return None
























# --- Cost ledger writers (issue #490) ------------------------------------------------------
# Durable, Stripe-joinable spend. PostHog is the fast analytics plane; these rows are the exact
# source of truth the margin report joins against MRR. High-volume LLM spend arrives here already
# rolled up (one row per user x feature x tier x day) — see observability.flush_llm_cost_rollup.








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






























































def has_engagement_preferences(user_id: int) -> bool:
    """True when the user has actually SAVED engagement preferences. get_engagement_preferences()
    returns code defaults for everyone, so only the row's existence proves they configured it.

    The two-valued view of `engagement_preferences_are_configured` for callers that only steer UI
    copy: an unreadable row reads as False, exactly as this has always behaved. A caller that would
    WRITE on the answer must use the three-valued function instead (issue #952).
    """
    return engagement_preferences_are_configured(user_id) is True






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





