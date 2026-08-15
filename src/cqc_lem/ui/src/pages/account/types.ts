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

/**
 * An EDITABLE row of a payload the SPA also writes back.
 *
 * The generated payload type is what the server SENDS, and every key of it is on the wire. The
 * roster and story-bank editors also build rows that have never been saved, whose automation-owned
 * columns (`id`, the counters, the follow/connect ladder) do not exist yet — so `Editable` keeps
 * exactly the fields the matching PUT writes required and lets the rest be absent, while every
 * NAME and TYPE still comes from the schema. Widening it to a plain `Partial` would be the bug
 * this file exists to prevent: the PUT replaces the whole row, so a droppable editable field is a
 * column a save resets.
 */
type Editable<Row, Written extends keyof Row> = Partial<Row> & Pick<Row, Written>

type RosterRow = GetDetail<'/api/user/engagement-targets'>['targets'][number]

/** One account on the curated engagement roster (issue #616).
 *
 *  `last_engaged_at` / `comments_this_week` and everything from `comment_blocked_streak` down are
 *  written by the roster pass and are read-only here (issues #962, #979) — `upsert_engagement_targets`
 *  writes only the fields below, so saving the roster can never reset a streak or a follow state. */
export type EngagementTarget = Editable<
  RosterRow,
  'profile_url' | 'name' | 'category' | 'max_comments_per_week' | 'active' | 'source'
>

export type EngagementTargetCategory = RosterRow['category']

/** A seed candidate for an empty roster — never saved, so it carries none of the counters. */
export type EngagementTargetSuggestion =
  GetDetail<'/api/user/engagement-targets'>['suggestions'][number]

// A target is badged once it has been un-commentable on this many consecutive visits — one visit
// that happened to render only reshares is not evidence. Mirrors
// db.ENGAGEMENT_TARGET_BLOCKED_BADGE_STREAK.
export const ROSTER_BLOCKED_BADGE_STREAK = 2

export const TARGET_CATEGORIES: { key: EngagementTargetCategory; label: string; hint: string }[] = [
  { key: 'peer', label: 'Peer', hint: 'Creators at your level — aim for ~50% of the roster' },
  { key: 'icp', label: 'ICP', hint: 'Buyers / prospects — aim for ~30%' },
  { key: 'creator', label: 'Large creator', hint: 'Big audiences you borrow reach from — ~20%' },
]

type StoryRow = GetDetail<'/api/user/story-bank'>['entries'][number]

/** One piece of the user's own raw material (issue #620). `used_count` / `last_used_at` are the
 *  rotation counters written by generation and are read-only here. */
export type StoryEntry = Editable<StoryRow, 'kind' | 'title' | 'body' | 'happened_at' | 'active'>

export type StoryKind = StoryRow['kind']

export const STORY_KINDS: { key: StoryKind; label: string; hint: string }[] = [
  { key: 'anecdote', label: 'Anecdote', hint: 'Something that actually happened to you' },
  { key: 'number', label: 'Number', hint: 'A real figure from your own work' },
  { key: 'opinion', label: 'Opinion', hint: 'A view you actually hold, ideally an unpopular one' },
  { key: 'client_win', label: 'Client win', hint: 'A real outcome you delivered' },
  { key: 'mistake', label: 'Mistake', hint: "Something you got wrong and what it cost" },
  { key: 'artifact', label: 'Artifact', hint: 'Something you built, shipped or wrote' },
]

/** One rung of a DM ladder. The PUT replaces the WHOLE set, so nothing here is droppable. */
export type DmTemplate = GetDetail<'/api/user/dm-templates'>[number]

/** The newsletter settings row. `newsletter_url` / `last_published_at` are written by the publish
 *  run rather than by the settings PUT, which round-trips them untouched. */
export type NewsletterSettings = GetDetail<'/api/user/newsletter-settings'>

export type NewsletterSubscribers = GetDetail<'/api/user/newsletter-subscribers'>

/** One growth snapshot (issue #400). `subscriber_count` is null when the page could not be read on
 *  that run — a different fact from zero subscribers. */
export type NewsletterSubscriberStat = NewsletterSubscribers['history'][number]

/** The owned-asset CTAs delivered in the same window (issue #624), so growth can be read against
 *  them. `newsletter_links` is null (not 0) when no subscribe URL is configured. */
export type ArtifactCtaAttribution = NewsletterSubscribers['attribution']

type NewsletterDraftPayload = GetDetail<'/api/user/newsletter-draft'>

/** One queued edition. `cover_image_url` (issue #893) is null when it has none, and
 *  `cover_image_status` 'approved' publishes with the edition while 'pending_review' waits for you.
 *
 *  The queue edits an edition field by field, so the cover and the format fields — which the
 *  drafting run writes, never the editor — are not required to build one. */
export type NewsletterEdition = Editable<
  NewsletterDraftPayload['editions'][number],
  'id' | 'title' | 'subtitle' | 'body' | 'status' | 'scheduled_for'
>

/** The review queue. `next_publish` is the slot AFTER the last edition already queued — when a NEW
 *  draft would go out, not when the next send is. */
export type NewsletterDraft = Editable<NewsletterDraftPayload, 'editions' | 'next_publish'>

export const WEEKDAYS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']

/** One joined LinkedIn group. `enabled` (commenting) and `post_enabled` (publishing, issue #769)
 *  are independent switches — being in a group is not permission to publish into it — and
 *  `is_next_post` is server-marked: the group the NEXT weekly group post goes to.
 *
 *  The card toggles the two switches per row, so only the identity fields are required to name one. */
export type UserGroup = Editable<
  GetDetail<'/api/user/groups'>[number],
  'group_id' | 'group_name' | 'enabled'
>

/** The group post waiting to be published, editable until it ships (issue #932).
 *
 *  `media_url` is null on a text-only draft (issue #1224), `best_practices` is the SAME list the
 *  drafting prompt was held to, and `can_undo_skip` / `undo_deadline` say whether "Skip this week"
 *  can still be reversed and when that window closes (issue #1415). */
export type GroupPostDraft = Editable<
  NonNullable<GetDetail<'/api/user/group-post-draft'>>,
  'id' | 'group_id' | 'group_name' | 'content' | 'status'
>

export type LeadMagnet = GetDetail<'/api/user/lead-magnet'>

export const DM_EVENTS: { key: string; label: string }[] = [
  { key: 'connection_accepted', label: 'Connection accepted' },
  { key: 'recommendation_received', label: 'Recommendation received' },
  { key: 'collaboration', label: 'After a collaboration' },
  { key: 'profile_viewer', label: 'Profile viewer outreach' },
  // Every event type the API stores has to be editable here: a save is the WHOLE set and the server
  // deletes what the payload leaves out (issue #1575), so an event this card never rendered would be
  // wiped by the next save of any other one.
  { key: 'manual', label: 'Manual outreach' },
  { key: 'funnel', label: 'Outreach funnel DM' },
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
