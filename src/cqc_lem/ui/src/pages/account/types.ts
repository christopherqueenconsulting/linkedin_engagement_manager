export type EngPrefs = {
  tone: string | null
  comment_length: string
  comment_style: string | null
  use_emojis: boolean
  use_hashtags: boolean
  include_topics: string[]
  exclude_topics: string[]
  include_keywords: string[]
  exclude_keywords: string[]
  include_authors: string[]
  exclude_authors: string[]
  // Round-tripped, not edited: the PUT model defaults these and the upsert writes the WHOLE row, so
  // omitting them from this type made every save from the SPA silently reset both columns (F2).
  post_types: string[]
  default_buyer_stage: string | null
  focus_topics: string[]
  business_goals: string | null
  personal_goals: string | null
  // Quality-gate sensitivity (issue #421). null = use the deploy default in gate_defaults.
  authenticity_score_min: number | null
  post_similarity_max_pct: number | null
  min_reactions: number | null
  max_post_age_hours: number | null
  reply_to_own_comments: boolean
  max_comments_per_day: number
  max_dms_per_day: number
  max_invites_per_day: number
  max_company_page_invites_per_day: number
  connection_request_mode: string
  connection_targeting_mode: string
  connection_target_authors: string[]
  min_connection_icp_score: number
  default_video_quality: string
  reply_check_mode: string
  reply_sweeps_per_day: number
  reply_max_post_age_days: number
  reply_inbound_address?: string | null
  gmail_forward_confirmation?: GmailForwardConfirmation | null
  feed_fallback_when_empty: boolean
  link_in_first_comment: boolean
  // How many day-type slots a week the content plan fills (issue #621). 7 = daily.
  posts_per_week: number
  // Which weekdays those slots may land on, Mon=0 … Sun=6 (issue #581). Default Mon-Fri.
  posting_days: number[]
  max_catchup_touches_per_day: number
  catchup_touch_mode: string
  catchup_event_types: string[]
  catchup_message_source: string
  // Read-only: the highest catch-up cap this plan allows (10/day is premium-only).
  max_catchup_touches_allowed?: number
  // Read-only: the deploy-wide gate thresholds used when the user hasn't set their own.
  gate_defaults?: { authenticity_score_min: number; post_similarity_max_pct: number }
  feed_reach?: FeedReach | null
  // Read-only: false when the user has never saved engagement preferences, so the hub can start a
  // brand-new account on the Balanced preset without ever touching an existing user's saved values.
  has_saved_preferences?: boolean
}

// What the /user/settings endpoint stores — one PUT for the whole object, so every control that
// edits any of these fields must share one piece of state (omitting a field resets it server-side).
export type UserPrefs = {
  last_login_inactivate_delay: number | null
  auto_schedule_posts: boolean
  content_language: string | null
  effective_content_language?: string | null
  content_buffer_days: number
  content_buffer_max_posts: number
}

export type GmailForwardConfirmation = {
  code?: string | null
  confirmed?: boolean
  url_found?: boolean
}

export type FeedReach = {
  examined: number
  passed_filters: number
  matched_topics: number
  commented: number
  fallback_used: boolean
  // Roster-sourced vs feed-sourced split + the on-topic gate's rejections (issue #616).
  roster_commented?: number
  feed_commented?: number
  roster_targets_visited?: number
  roster_examined?: number
  off_topic_skipped?: number
  max_post_age_hours?: number
  min_reactions?: number
  at?: string
}

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
}

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
