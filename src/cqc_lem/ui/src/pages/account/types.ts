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
  focus_topics: string[]
  business_goals: string | null
  personal_goals: string | null
  min_reactions: number | null
  max_post_age_hours: number | null
  reply_to_own_comments: boolean
  max_comments_per_day: number
  max_dms_per_day: number
  max_invites_per_day: number
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
  max_catchup_touches_per_day: number
  catchup_touch_mode: string
  catchup_event_types: string[]
  catchup_message_source: string
  // Read-only: the highest catch-up cap this plan allows (10/day is premium-only).
  max_catchup_touches_allowed?: number
  feed_reach?: FeedReach | null
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
  max_post_age_hours?: number
  min_reactions?: number
  at?: string
}

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

export type NewsletterSubscribers = {
  latest: number | null
  history: NewsletterSubscriberStat[]
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
