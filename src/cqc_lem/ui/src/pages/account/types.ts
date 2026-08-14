// Everything below that names a real endpoint is GENERATED, not written here (issue #1446): the
// server dumps `/api/openapi.json`, `npm run gen:api-types` turns it into `src/api/schema.ts`, and
// these aliases pick the payload out of it. The hand-maintained copies these replaced drifted the
// expensive way — `post_types` and `default_buyer_stage` were once missing from `EngPrefs`, and
// because the PUT writes the WHOLE row, every save from the SPA silently reset both columns (F2).
import type { components } from '../../api/schema'
import type { GetDetail } from '../../api/types'

/** The saved engagement preferences plus the read-only context the hub renders.
 *
 *  Anything the server derives per request (`gate_defaults`, `feed_reach`,
 *  `max_catchup_touches_allowed`, `has_saved_preferences`, the catch-up bounds) is round-tripped on
 *  a save and ignored server-side; the rest is a stored column. */
export type EngPrefs = GetDetail<'/api/user/engagement-preferences'>

/** The account-level preferences the Account page edits — one PUT for the whole object, so every
 *  control that edits any of these fields must share one piece of state (omitting a field resets it
 *  server-side). `effective_content_language` is derived, so it is not part of the edit state. */
export type UserPrefs = Omit<
  NonNullable<GetDetail<'/api/user/settings'>['preferences']>,
  'effective_content_language'
>

/** The whole `GET /user/settings` payload — subscription, preferences, blog/sitemap, company page. */
export type UserSettings = GetDetail<'/api/user/settings'>

export type GmailForwardConfirmation = components['schemas']['GmailForwardConfirmation']

export type FeedReach = components['schemas']['FeedReach']

export type EngagementTargetCategory = 'peer' | 'icp' | 'creator'

// One account on the curated engagement roster. last_engaged_at / comments_this_week are written
// by the commenting task and are read-only here.
export type EngagementTarget = {
  id?: number
  profile_url: string
  name: string | null
  category: EngagementTargetCategory
  max_comments_per_week: number
  active: boolean
  source: 'user' | 'suggested'
  last_engaged_at?: string | null
  comments_this_week?: number
  // Written by the roster pass, read-only here (issue #962). A streak counts consecutive VISITS
  // where the target's posts rendered with no comment affordance at all — the signature of an
  // author who only accepts comments from connections or followers.
  comment_blocked_streak?: number
  last_blocked_at?: string | null
  // 'following' and 'follow_failed' are terminal: the roster never re-examines the target.
  follow_status?: 'unknown' | 'not_following' | 'following' | 'follow_failed'
  followed_at?: string | null
  follow_attempts?: number
  // The rung above follow (issue #979): set once a FOLLOWED target is still un-commentable on a
  // later visit. 'requested' / 'connected' / 'failed' are all terminal for automation — one invite
  // per target, ever.
  connect_status?: 'unknown' | 'needs_connection' | 'requested' | 'connected' | 'failed'
  connect_requested_at?: string | null
}

// A target is badged once it has been un-commentable on this many consecutive visits — one visit
// that happened to render only reshares is not evidence. Mirrors
// db.ENGAGEMENT_TARGET_BLOCKED_BADGE_STREAK.
export const ROSTER_BLOCKED_BADGE_STREAK = 2

export const TARGET_CATEGORIES: { key: EngagementTargetCategory; label: string; hint: string }[] = [
  { key: 'peer', label: 'Peer', hint: 'Creators at your level — aim for ~50% of the roster' },
  { key: 'icp', label: 'ICP', hint: 'Buyers / prospects — aim for ~30%' },
  { key: 'creator', label: 'Large creator', hint: 'Big audiences you borrow reach from — ~20%' },
]

export type StoryKind =
  | 'anecdote'
  | 'number'
  | 'opinion'
  | 'client_win'
  | 'mistake'
  | 'artifact'

// One piece of the user's own raw material (issue #620). used_count / last_used_at are the rotation
// counters written by generation and are read-only here.
export type StoryEntry = {
  id?: number
  kind: StoryKind
  title: string | null
  body: string
  happened_at: string | null
  active: boolean
  used_count?: number
  last_used_at?: string | null
}

export const STORY_KINDS: { key: StoryKind; label: string; hint: string }[] = [
  { key: 'anecdote', label: 'Anecdote', hint: 'Something that actually happened to you' },
  { key: 'number', label: 'Number', hint: 'A real figure from your own work' },
  { key: 'opinion', label: 'Opinion', hint: 'A view you actually hold, ideally an unpopular one' },
  { key: 'client_win', label: 'Client win', hint: 'A real outcome you delivered' },
  { key: 'mistake', label: 'Mistake', hint: "Something you got wrong and what it cost" },
  { key: 'artifact', label: 'Artifact', hint: 'Something you built, shipped or wrote' },
]

export type DmTemplate = {
  event_type: string
  step: number
  delay_hours: number
  template_text: string
  is_active: boolean
}

export type NewsletterSettings = {
  enabled: boolean
  title: string | null
  topic: string | null
  cadence: string
  align_with_blog: boolean
  publish_day: number
  publish_hour: number
  generate_lead_days: number
  max_queued_drafts: number
  invite_connections_enabled: boolean
  max_invites_per_run: number
  /** Opt-in: generate a cover image for each new draft (costs money per edition). */
  cover_image_auto: boolean
}

export type NewsletterSubscriberStat = {
  subscriber_count: number | null
  invites_sent: number
  captured_at: string
}

export type ArtifactCtaAttribution = {
  window_days: number
  lead_magnet_dms: number
  // null (not 0) when no subscribe URL is configured — there was nothing to carry.
  newsletter_links: number | null
}

export type NewsletterSubscribers = {
  latest: number | null
  history: NewsletterSubscriberStat[]
  attribution?: ArtifactCtaAttribution
}

export type NewsletterEdition = {
  id: number
  title: string | null
  subtitle: string | null
  subject?: string | null
  format?: string | null
  hook_style?: string | null
  body: string | null
  status: string
  scheduled_for: string | null
  /** Cover image (issue #893) — null when the edition has none. */
  cover_image_url?: string | null
  cover_image_source?: 'upload' | 'ai' | null
  /** 'approved' publishes with the edition; 'pending_review' is a generated cover awaiting you. */
  cover_image_status?: 'pending_review' | 'approved' | null
}

export type NewsletterDraft = {
  editions: NewsletterEdition[]
  next_publish: string | null
  max_queued_drafts?: number
  generate_lead_days?: number
}

export const WEEKDAYS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']

export type UserGroup = {
  group_id: string
  group_name: string | null
  enabled: boolean
  /** Whether the weekly original group post may land in this group (issue #769). */
  post_enabled?: boolean
  last_posted_at?: string | null
  /** Server-marked: the group the NEXT weekly group post goes to. */
  is_next_post?: boolean
}

/** The group post waiting to be published, editable until it ships (issue #932). */
export type GroupPostDraft = {
  id: number
  group_id: string
  group_name: string | null
  content: string
  status: string
  created_at?: string | null
  // Attached image/video (issue #1224). media_url is null on a text-only group post.
  media_url?: string | null
  media_type?: 'image' | 'video' | null
  // Served with the draft so the editor shows the same rules the drafting prompt follows.
  best_practices?: string[]
  /** Whether "Skip this week" can still be undone — false once the publish slot passed (issue #1415). */
  can_undo_skip?: boolean
  /** The publish slot the undo window closes at (ISO, UTC). */
  undo_deadline?: string | null
}

export type LeadMagnet = {
  enabled: boolean
  keyword: string | null
  message: string | null
}

export const DM_EVENTS: { key: string; label: string }[] = [
  { key: 'connection_accepted', label: 'Connection accepted' },
  { key: 'recommendation_received', label: 'Recommendation received' },
  { key: 'collaboration', label: 'After a collaboration' },
  { key: 'profile_viewer', label: 'Profile viewer outreach' },
  // The direction for the next message after a lead REPLIES (issue #485). The draft is written
  // against what they actually said; this template sets its intent, not its wording.
  { key: 'nurture', label: 'After they reply (nurture)' },
  // Catch-up milestone congratulations (issue #482) — these templates also accept {event_detail}.
  { key: 'job_change', label: 'Catch-up · New job' },
  { key: 'promotion', label: 'Catch-up · Promotion' },
  { key: 'work_anniversary', label: 'Catch-up · Work anniversary' },
  { key: 'education', label: 'Catch-up · Education milestone' },
  { key: 'in_the_news', label: 'Catch-up · In the news' },
  { key: 'birthday', label: 'Catch-up · Birthday' },
]

// Milestone types the Catch-up scan can congratulate, ordered by BD value (issue #482).
export const CATCHUP_EVENTS: { key: string; label: string }[] = [
  { key: 'job_change', label: 'New job' },
  { key: 'promotion', label: 'Promotion' },
  { key: 'work_anniversary', label: 'Work anniversary' },
  { key: 'education', label: 'Education milestone' },
  { key: 'in_the_news', label: 'In the news' },
  { key: 'birthday', label: 'Birthday' },
]

export const csv = (arr: string[] | undefined | null) => (arr && arr.length ? arr.join(', ') : '')
export const parseCsv = (s: string) => s.split(',').map((x) => x.trim()).filter(Boolean)
