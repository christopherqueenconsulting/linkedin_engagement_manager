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
  min_reactions: number | null
  max_post_age_hours: number | null
  reply_to_own_comments: boolean
  max_comments_per_day: number
  max_dms_per_day: number
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
}

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
]

export const csv = (arr: string[] | undefined | null) => (arr && arr.length ? arr.join(', ') : '')
export const parseCsv = (s: string) => s.split(',').map((x) => x.trim()).filter(Boolean)
