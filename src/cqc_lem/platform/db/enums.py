"""The status vocabularies the schema enforces.

MySQL ENUM columns back most of these, so adding a member needs a Flyway migration — see
`compose/local/database/migrations/README.md`. Pure values: nothing here touches a connection,
which is why it is the one db module safe to import from anywhere.
"""

from enum import StrEnum


class PostType(StrEnum):
    """The `posts.post_type` ENUM, mirrored in Python so post types are never raw strings.

    `posts.post_type` is a MySQL ENUM, so adding a member here is only half the change — the column needs
    a Flyway migration before anything can be written with the new value.
    """
    TEXT = 'text'
    CAROUSEL = 'carousel'
    VIDEO = 'video'
    DOCUMENT = 'document'  # native PDF/document post — highest-reach 2026 format


class PostStatus(StrEnum):
    """The `posts.status` ENUM. `error` means generation or posting failed and the row needs a human.

    Same rule as `PostType`: the column is a MySQL ENUM, so a new member lands in a Flyway migration
    first.
    """
    PLANNING = 'planning'
    PENDING = 'pending'
    APPROVED = 'approved'
    REJECTED = 'rejected'
    SCHEDULED = 'scheduled'
    POSTED = 'posted'
    ERROR = 'error'  # generation/posting failed (e.g. no real carousel images) — needs manual/dev fix


class ScheduledDmStatus(StrEnum):
    """Status for a scheduled 1:1 DM (issue #306), mirroring PostStatus."""
    PENDING = 'pending'      # draft awaiting approval
    APPROVED = 'approved'    # approved, waiting for its scheduled_time
    SCHEDULED = 'scheduled'  # scanner dispatched the send task
    SENT = 'sent'            # delivered
    FAILED = 'failed'        # send failed
    CANCELED = 'canceled'    # canceled before send


class ConnectionRequestStatus(StrEnum):
    """Status for a proactive, approval-gated connection request (issue #398), mirroring ScheduledDmStatus."""
    PENDING = 'pending'      # draft awaiting approval
    APPROVED = 'approved'    # approved, waiting for the scanner
    SENDING = 'sending'      # scanner dispatched the send task
    SENT = 'sent'            # invitation sent
    FAILED = 'failed'        # send failed
    CANCELED = 'canceled'    # canceled before send


class CatchupEventType(StrEnum):
    """A LinkedIn Catch-up "moment" we can congratulate on (issue #482). Ordered most→least
    BD-relevant: a new job or promotion is a real trigger event, a birthday is small talk.
    """
    JOB_CHANGE = 'job_change'
    PROMOTION = 'promotion'
    WORK_ANNIVERSARY = 'work_anniversary'
    EDUCATION = 'education'
    IN_THE_NEWS = 'in_the_news'
    BIRTHDAY = 'birthday'


class CatchupTouchStatus(StrEnum):
    """Status of a drafted catch-up congratulations DM (issue #482), mirroring ConnectionRequestStatus."""
    PENDING = 'pending'      # drafted, awaiting human approval
    APPROVED = 'approved'    # approved, waiting for the capped scanner
    SENDING = 'sending'      # scanner dispatched the send task
    SENT = 'sent'            # DM sent
    SKIPPED = 'skipped'      # scored below the bar / event type disabled — kept as a dedup tombstone
    FAILED = 'failed'        # send failed
    CANCELED = 'canceled'    # operator canceled before send


class GroupPostDraftStatus(StrEnum):
    """Status of the weekly group post's draft (issue #932). The draft is written days before the
    publish slot so the user can read and revise it — silence ships it, which is why the resting
    state is READY rather than a pending-approval one.
    """
    READY = 'ready'          # drafted (and editable) — publishes at the weekly slot unless skipped
    SKIPPED = 'skipped'      # the user cancelled this week's post, or its group stopped taking posts
    PUBLISHED = 'published'  # it shipped into the group
    FAILED = 'failed'        # the run reached the group and the group would not take a member post

    @classmethod
    def user_settable(cls) -> "tuple[GroupPostDraftStatus, ...]":
        """The statuses the SPA may write (issue #1224).

        READY and SKIPPED are the two ends of the user's own decision — ship it this week or don't —
        and either can be undone until the publish run takes the draft. PUBLISHED and FAILED are
        RECORDS of what the run did, so accepting one from a client would let the queue claim a post
        that never shipped.
        """
        return (cls.READY, cls.SKIPPED)


class GroupPostMediaType(StrEnum):
    """What a group post draft's attached media IS (issue #1224).

    Derived from the stored file's extension when the media is attached, never taken from the
    client, because it is what tells the publish run whether it is handing LinkedIn's group composer
    an image or a video. No member for "text": a draft with no media has `media_url IS NULL`, the
    same way a text post carries no `posts.image_url`.
    """
    IMAGE = 'image'
    VIDEO = 'video'


class OutreachStage(StrEnum):
    """Stage of a comment-first outreach funnel target (issue #399)."""
    COMMENT = 'comment'      # leave a value-adding comment on the prospect's post
    CONNECT = 'connect'      # send a connection request (with a note)
    DM = 'dm'                # send the voice-aligned DM (must be a 1st-degree connection)
    COMPLETED = 'completed'  # terminal: the DM fired


class OutreachStatus(StrEnum):
    """Status of the current funnel stage (issue #399), mirroring the approval-gated DM lifecycle."""
    PENDING = 'pending'      # draft awaiting human approval
    APPROVED = 'approved'    # human approved; the processor will fire this stage
    ACTED = 'acted'          # terminal: the final (dm) stage fired
    SKIPPED = 'skipped'      # current stage skipped without firing
    FAILED = 'failed'        # firing the stage errored
    CANCELED = 'canceled'    # operator canceled the whole funnel for this target


class LeadSignalSource(StrEnum):
    """Which existing read path caught an inbound buying signal (issue #483)."""
    POST_COMMENT = 'post_comment'    # a comment on the user's OWN post
    COMMENT_REPLY = 'comment_reply'  # a reply to a comment WE left on someone else's post
    DM = 'dm'                        # an inbound DM reply in a thread we already open


class LeadSignalChannel(StrEnum):
    """How an approved hot-lead response is delivered (issue #483)."""
    REPLY = 'reply'  # post the draft under their comment at context_url
    DM = 'dm'        # send the draft as a private message


class LeadSignalStatus(StrEnum):
    """Lifecycle of a detected hot lead (issue #483), mirroring the approval-gated DM lifecycle."""
    NEW = 'new'              # draft awaiting human approval
    APPROVED = 'approved'    # human approved; the responder will deliver it
    SENT = 'sent'            # response delivered
    DISMISSED = 'dismissed'  # operator dismissed the signal
    FAILED = 'failed'        # delivery errored


class CostCategory(StrEnum):
    """Kind of spend a `cost_ledger` row records (issue #490, docs/cost-performance-margin-plan.md §A.1)."""
    LLM = 'llm'              # inference through LiteLLM (rolled up daily per user x feature x tier)
    MEDIA = 'media'          # video renders (Runway) and generated images (gpt-image / FLUX)
    PROXY = 'proxy'          # per-user residential / amortized regional egress proxy
    INFRA = 'infra'          # VPS + containers, amortized across active users
    EMAIL = 'email'          # transactional sends
    GEOCODING = 'geocoding'  # location lookups
    POSTHOG = 'posthog'      # our own analytics ingestion


class LeadStage(StrEnum):
    """Warmth of a scored lead in the CRM-lite pipeline (issue #484), coldest first."""
    COLD = 'cold'                        # in our orbit, no meaningful recent signal
    WARM = 'warm'                        # engaging often enough to be worth nurturing
    HOT = 'hot'                          # strong ICP fit + heavy engagement, or an unanswered buying question
    IN_CONVERSATION = 'in_conversation'  # a live DM thread with someone who also engages with us
    OPPORTUNITY = 'opportunity'          # they asked a buying question and we are answering it


class LeadSignalKind(StrEnum):
    """The engagement signals a lead score is built from (issue #484). Every one is read from data
    the automation already records — no new scraping.
    """
    ENGAGED = 'engaged'            # commented/reacted on one of our posts (post_engagers)
    INTENT = 'intent'              # raised a buying signal (lead_signals, issue #483)
    DM = 'dm'                      # we sent them a DM (scheduled_dms / dm_followups)
    PROFILE_VIEW = 'profile_view'  # they viewed our profile (dm_followups, profile_viewer event)
    CONNECT = 'connect'            # we sent them a connection request (connection_requests)
    FUNNEL = 'funnel'              # they are in the comment->connect->DM funnel (issue #399)


class FeedbackSource(StrEnum):
    """Where a piece of user feedback came in from (issue #496). Only WIDGET is captured today —
    the rest are the channels the feedback->auto-work loop will add later.
    """
    WIDGET = 'widget'    # the in-app feedback/bug widget
    BUG = 'bug'          # a bug report raised outside the widget (e.g. support email)
    NPS = 'nps'          # an NPS survey response
    REVIEW = 'review'    # a public review (marketplace/G2/etc.)
    PASSIVE = 'passive'  # inferred from behavior, not typed by the user
    CSAT = 'csat'        # a "did this fix it?" answer after a shipped fix (issue #502)


class FeedbackStatus(StrEnum):
    """Lifecycle of a feedback item as it moves through the auto-work loop (issue #496)."""
    NEW = 'new'                      # just captured, not looked at
    TRIAGED = 'triaged'              # reviewed/classified
    CLUSTERED = 'clustered'          # grouped with similar reports
    ISSUE_CREATED = 'issue_created'  # a GitHub issue was opened for its cluster
    RESOLVED = 'resolved'            # shipped/answered
    DISMISSED = 'dismissed'          # not actionable


class FaqStatus(StrEnum):
    """Lifecycle of a public FAQ answer (issue #506). Only PUBLISHED rows are served on the front
    page — an auto-generated answer lands as DRAFT until it is reviewed.
    """
    PUBLISHED = 'published'
    DRAFT = 'draft'
    ARCHIVED = 'archived'


class AffiliateStatus(StrEnum):
    """(A) affiliate STATUS (issue #737) — whether the user holds a referral link and earns trial
    time for it. Default ENROLLED, one click to OPTED_OUT. This says nothing at all about (B),
    whether LEM may publish promo content from their account; that is `promo_content_opt_in`, is
    default-off, and is stored beside this column precisely so the two can never be conflated.
    """
    ENROLLED = 'enrolled'
    OPTED_OUT = 'opted_out'


class ReferralStatus(StrEnum):
    """A referral's lifecycle. PENDING on signup through a member's link; CONVERTED only once the
    referred user ACTIVATES (a real activated signup, not a click); REJECTED for self-referral and
    the other fraud shapes — stored rather than dropped so the signal is countable.
    """
    PENDING = 'pending'
    CONVERTED = 'converted'
    REJECTED = 'rejected'


class AffiliateRewardKind(StrEnum):
    """What a `affiliate_rewards` row paid for. ENROLLMENT is the status-linked bonus (revoked on
    opt-out via a negative REVOKED row); REFERRAL was earned by driving an activation and is never
    clawed back.
    """
    ENROLLMENT = 'enrollment'
    REFERRAL = 'referral'
    REVOKED = 'revoked'


class OnboardingStep(StrEnum):
    """Steps of the activation checklist (issue #500), in the order a user completes them.
    ACTIVATED is the "aha" moment: first AI post published AND first automated comment/DM sent.
    """
    LINKEDIN_CONNECTED = 'linkedin_connected'
    VOICE_SET = 'voice_set'
    FIRST_POST_APPROVED = 'first_post_approved'
    CAPS_ENABLED = 'caps_enabled'
    ACTIVATED = 'activated'


class LogActionType(StrEnum):
    """The `logs.action_type` ENUM — what an automation run did, one row per attempt.

    These rows are not only history: the per-day caps and the dedup checks are COUNTED off them
    (`count_comments_today`, `count_invites_sent_today`, `has_engaged_url_with_x_days`), so an action
    whose log row never landed is budget the account spent and will spend again. Extending this needs a
    Flyway migration on the ENUM column (V16 and V37 are what that looks like).
    """
    COMMENT = 'comment'
    DM = 'dm'
    REPLY = 'reply'
    POST = 'post'
    ENGAGED = 'engaged'
    FOLLOWUP = 'followup'


class LogResultType(StrEnum):
    """The `logs.result` ENUM. Every cap and dedup counter filters on SUCCESS, so a FAILURE row buys no budget."""
    SUCCESS = 'success'
    FAILURE = 'failure'


class AuthAuditEvent(StrEnum):
    """Every security-relevant thing that can happen to an account's identity."""
    LOGIN_SUCCESS = "login_success"
    LOGIN_FAILED = "login_failed"
    LOGIN_RATE_LIMITED = "login_rate_limited"
    PIN_LOCKED = "pin_locked"
    LOGOUT = "logout"
    SESSION_REVOKED = "session_revoked"
    AGENT_TOKEN_MINTED = "agent_token_minted"
    SESSIONS_REVOKED_ALL = "sessions_revoked_all"
    EMAIL_CHANGE_REQUESTED = "email_change_requested"
    EMAIL_CHANGED = "email_changed"
    # Strong authentication (2c)
    FACTOR_ADDED = "factor_added"
    FACTOR_REMOVED = "factor_removed"
    SECOND_FACTOR_REQUIRED = "second_factor_required"
    SECOND_FACTOR_FAILED = "second_factor_failed"
    RECOVERY_CODES_GENERATED = "recovery_codes_generated"
    RECOVERY_CODE_USED = "recovery_code_used"
    STEP_UP_VERIFIED = "step_up_verified"
    STEP_UP_DENIED = "step_up_denied"
    # A scoped session (extension / enroll) was used outside its surface (2c.1). For an extension
    # token this is the clearest signal available that someone else is holding it — the extension
    # itself only ever calls one path, so it can never produce this row by accident.
    SESSION_SCOPE_DENIED = "session_scope_denied"
    # A signed-in caller named ANOTHER account as the target of an /api call (#914). The SPA cannot
    # produce this — it sends the caller's own address or nothing at all — so a row here is a broken
    # client or somebody working the hole that issue closed, and it is the highest-signal thing this
    # boundary emits. `details` carries the KIND of identifier and the path, never the value: the
    # caller-supplied half is somebody else's address and the audit log is not where it accumulates.
    FOREIGN_TARGET_DENIED = "foreign_target_denied"
    # An admin granted or removed another account's admin role from `/admin/users` (#1450). Keyed
    # on the TARGET account like every other event here — the acting admin rides in `details` as
    # `actor_user_id` — so the person it happened to sees it in their own Security card. No
    # migration: `auth_audit_log.event` is VARCHAR(50), not an ENUM.
    ADMIN_GRANTED = "admin_granted"
    ADMIN_REVOKED = "admin_revoked"
    # Per-user disable and the one-time subscription comp (#1603, Phase 2 of #1450). Same keying:
    # the TARGET account in `user_id`, the acting admin in `details.actor_user_id`.
    ADMIN_USER_DISABLED = "admin_user_disabled"
    ADMIN_USER_ENABLED = "admin_user_enabled"
    ADMIN_SUBSCRIPTION_GRANTED = "admin_subscription_granted"


class FollowStatus(StrEnum):
    """Follow state of a roster target (issue #962) — the ONE vocabulary, shared by the MySQL ENUM,
    the DOM reading the resolver returns, and every write site, so a typo is an import error instead
    of a MySQL error at 3am. `StrEnum`, so a raw column value read back from the DB compares equal
    to a member without a conversion at every boundary.
    """
    UNKNOWN = 'unknown'                # we could not read the card — never "there is nothing to follow"
    NOT_FOLLOWING = 'not_following'
    FOLLOWING = 'following'
    FOLLOW_FAILED = 'follow_failed'    # the control never flipped, twice


class ConnectStatus(StrEnum):
    """Connect state of a roster target (issue #979) — the ONE vocabulary, shared by the MySQL ENUM,
    the DOM reading `_resolve_connect_state` returns, and every write site, exactly as
    `FollowStatus` is for the follow rung.

    The rung above follow: it is only ever reached by a target that IS followed and is STILL
    un-commentable, so 'needs_connection' is a claim backed by evidence rather than a guess about
    someone's privacy settings.
    """
    UNKNOWN = 'unknown'                  # nothing known / nothing to do — the resting state
    NEEDS_CONNECTION = 'needs_connection'
    REQUESTED = 'requested'
    CONNECTED = 'connected'
    FAILED = 'failed'                    # the invite could not be sent — never auto-retried
