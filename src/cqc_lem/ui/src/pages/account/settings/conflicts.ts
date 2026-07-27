import type { EngPrefs, LeadMagnet, NewsletterSettings, UserPrefs } from '../types'
import { FIELD_LIMITS } from '../fieldLimits'
import type { SectionKey } from './sections'

// The conflict matrix from docs/SETTINGS_IA_RESEARCH.md §4, as ONE pure function so every rule is
// unit-testable without rendering a thing. Three tiers (§6):
//   inform — correct but non-obvious behaviour. Grey, always visible.
//   warn   — this combination silently produces nothing (or much less than the user thinks). Amber,
//            inline next to the offending control, NEVER blocks a save.
//   block  — the write itself would fail. Blocking client-side means the user sees WHICH field is
//            wrong instead of losing the whole single-row upsert (the V52 failure mode).
export type Severity = 'inform' | 'warn' | 'block'

export type Finding = {
  /** Matrix row id (C1…C26) or `B:<field>` for a blocking validation. Stable — tests key off it. */
  id: string
  severity: Severity
  section: SectionKey
  /** The setting key this renders next to. */
  anchor: string
  message: string
  /** One-click fix: a patch to merge into the engagement preferences. */
  fix?: { label: string; patch: Partial<EngPrefs> }
}

export type ConflictContext = {
  eng: EngPrefs
  user: UserPrefs | null
  newsletter: NewsletterSettings | null
  leadMagnet: LeadMagnet | null
  videoCredits: number
  /** The timezone saved on the account, and the one the browser is actually in. */
  timezone: string | null
  browserTimezone: string | null
  /** Highest catch-up cap this plan allows (10/day is premium-only). */
  catchupAllowed: number
}

const notEmpty = (v: string[] | null | undefined) => !!v && v.length > 0

const hasIncludeFilter = (eng: EngPrefs) =>
  notEmpty(eng.include_topics) || notEmpty(eng.include_keywords) || notEmpty(eng.include_authors)

const norm = (s: string) => s.trim().toLowerCase()

/** Terms the user both steers content toward and excludes from their feed — excludes always win. */
export function overlappingExclusions(eng: EngPrefs): string[] {
  const wanted = new Set([...(eng.focus_topics ?? []), ...(eng.include_topics ?? [])].map(norm))
  const excluded = [...(eng.exclude_topics ?? []), ...(eng.exclude_keywords ?? [])]
  return [...new Set(excluded.filter((t) => wanted.has(norm(t))))]
}

const inRange = (v: number | null | undefined, lo: number, hi: number) =>
  v === null || v === undefined || (Number.isFinite(v) && v >= lo && v <= hi)

/** Values that would be rejected (or silently clamped) server-side. These DO block the save. */
export function blockingIssues(eng: EngPrefs): Finding[] {
  const out: Finding[] = []
  const tooLong = (v: string | null, limit: number) => !!v && v.length > limit
  if (tooLong(eng.tone, FIELD_LIMITS.tone))
    out.push({ id: 'B:tone', severity: 'block', section: 'voice', anchor: 'tone',
      message: `Tone is over ${FIELD_LIMITS.tone} characters. Shorten it — the whole settings row is saved at once, so an over-long value would roll back every other change too.` })
  if (tooLong(eng.comment_style, FIELD_LIMITS.comment_style))
    out.push({ id: 'B:comment_style', severity: 'block', section: 'voice', anchor: 'comment_style',
      message: `Style guidance is over ${FIELD_LIMITS.comment_style} characters.` })
  if (tooLong(eng.business_goals, FIELD_LIMITS.goals))
    out.push({ id: 'B:business_goals', severity: 'block', section: 'voice', anchor: 'business_goals',
      message: `Business goals are over ${FIELD_LIMITS.goals} characters.` })
  if (tooLong(eng.personal_goals, FIELD_LIMITS.goals))
    out.push({ id: 'B:personal_goals', severity: 'block', section: 'voice', anchor: 'personal_goals',
      message: `Personal goals are over ${FIELD_LIMITS.goals} characters.` })

  const numeric: [string, number | null | undefined, number, number, SectionKey, string][] = [
    ['min_reactions', eng.min_reactions, 0, 100000, 'targeting', 'Minimum reactions must be 0 or more.'],
    ['max_post_age_hours', eng.max_post_age_hours, 1, 720, 'targeting', 'Max post age must be between 1 and 720 hours.'],
    ['max_comments_per_day', eng.max_comments_per_day, 0, 100, 'volume', 'Comments per day must be between 0 and 100.'],
    ['max_dms_per_day', eng.max_dms_per_day, 0, 100, 'volume', 'DMs per day must be between 0 and 100.'],
    ['max_invites_per_day', eng.max_invites_per_day, 0, 100, 'volume', 'Invites per day must be between 0 and 100.'],
    ['max_company_page_invites_per_day', eng.max_company_page_invites_per_day, 0, 50, 'volume', 'Company-page invites per day must be between 0 and 50.'],
    ['reply_sweeps_per_day', eng.reply_sweeps_per_day, 2, 12, 'volume', 'Reply checks per day must be between 2 and 12.'],
    ['reply_max_post_age_days', eng.reply_max_post_age_days, 1, 14, 'volume', 'Reply look-back must be between 1 and 14 days.'],
    ['min_connection_icp_score', eng.min_connection_icp_score, 0, 100, 'outreach', 'ICP fit score must be between 0 and 100.'],
    ['authenticity_score_min', eng.authenticity_score_min, 0, 100, 'content', 'Minimum authenticity score must be between 0 and 100.'],
    ['post_similarity_max_pct', eng.post_similarity_max_pct, 10, 100, 'content', 'Max overlap must be between 10% and 100%.'],
    ['posts_per_week', eng.posts_per_week, 2, 7, 'content', 'Posts per week must be between 2 and 7.'],
  ]
  for (const [anchor, value, lo, hi, section, message] of numeric) {
    if (!inRange(value, lo, hi))
      out.push({ id: `B:${anchor}`, severity: 'block', section, anchor, message })
  }
  return out
}

export function evaluateConflicts(ctx: ConflictContext): Finding[] {
  const { eng, user, newsletter, leadMagnet, videoCredits, timezone, browserTimezone, catchupAllowed } = ctx
  const out: Finding[] = blockingIssues(eng)
  const includes = hasIncludeFilter(eng)
  const reach = eng.feed_reach

  // C1 — targeting so strict the last real scan matched nothing.
  if (includes && reach && reach.examined > 0 && reach.matched_topics === 0) {
    out.push({
      id: 'C1', severity: 'warn', section: 'targeting', anchor: 'include_topics',
      message: `Your last feed scan examined ${reach.examined} posts and matched 0 of them. Loosen your include topics/keywords, lower minimum reactions, or widen max post age.`,
      ...(eng.min_reactions ? { fix: { label: 'Clear minimum reactions', patch: { min_reactions: 0 } } } : {}),
    })
  }
  // C2 — includes set with the safety net off: nothing gets commented on a sparse day.
  if (includes && !eng.feed_fallback_when_empty) {
    out.push({
      id: 'C2', severity: 'warn', section: 'targeting', anchor: 'feed_fallback_when_empty',
      message: 'On days when nothing in your feed matches these filters, LEM will comment on nothing at all.',
      fix: { label: 'Turn the fallback on', patch: { feed_fallback_when_empty: true } },
    })
  }
  // C3 — the fallback is a no-op without an include filter (F6).
  if (!includes && eng.feed_fallback_when_empty) {
    out.push({
      id: 'C3', severity: 'inform', section: 'targeting', anchor: 'feed_fallback_when_empty',
      message: "You have no include filters, so there is nothing to fall back from — this only applies once you add include topics, keywords or authors.",
    })
  }
  // C4 — commenting off makes all targeting dead config.
  if (eng.max_comments_per_day === 0) {
    out.push({
      id: 'C4', severity: 'warn', section: 'volume', anchor: 'max_comments_per_day',
      message: 'Commenting is off, so nothing in "Who I Engage" has any effect.',
      fix: { label: 'Allow 8 comments/day', patch: { max_comments_per_day: 8 } },
    })
  }
  // C5 — cadence is only read in scheduled mode.
  if (eng.reply_check_mode !== 'scheduled' && (eng.reply_sweeps_per_day ?? 2) !== 2) {
    out.push({
      id: 'C5', severity: 'inform', section: 'volume', anchor: 'reply_check_mode',
      message: 'Your reply-check cadence is only used in Scheduled mode — it is ignored right now.',
    })
  }
  // C6 — event mode without confirmed Gmail forwarding replies to nothing, silently.
  if (eng.reply_check_mode === 'event' && !eng.gmail_forward_confirmation?.confirmed) {
    out.push({
      id: 'C6', severity: 'warn', section: 'volume', anchor: 'reply_check_mode',
      message: 'Email forwarding is not confirmed yet, so replies to comments on your posts will never fire. Finish the 3-step setup below.',
    })
  }
  // C7 — scheduled sweeps open a browser session per run.
  if (eng.reply_check_mode === 'scheduled') {
    out.push({
      id: 'C7', severity: 'warn', section: 'volume', anchor: 'reply_check_mode',
      message: `Scheduled checks open a LinkedIn session ${eng.reply_sweeps_per_day ?? 2}× a day, which can get your account temporarily rate-limited (HTTP 429). Event-driven avoids that entirely.`,
      fix: { label: 'Switch to event-driven', patch: { reply_check_mode: 'event' } },
    })
  }
  // C8 — catch-up alone can eat the whole DM budget (both come out of max_dms_per_day).
  const catchupCap = eng.max_catchup_touches_per_day ?? 0
  if (catchupCap > 0 && eng.max_dms_per_day > 0 && catchupCap >= eng.max_dms_per_day) {
    out.push({
      id: 'C8', severity: 'warn', section: 'volume', anchor: 'max_catchup_touches_per_day',
      message: `Catch-up congratulations come out of your DM budget, and ${catchupCap}/day is your entire budget of ${eng.max_dms_per_day} — appreciation, outreach and nurture DMs would never send.`,
      fix: { label: `Raise DMs/day to ${catchupCap + 5}`, patch: { max_dms_per_day: catchupCap + 5 } },
    })
  }
  // C9 — DMs off with templates configured.
  if (eng.max_dms_per_day === 0) {
    out.push({
      id: 'C9', severity: 'warn', section: 'volume', anchor: 'max_dms_per_day',
      message: 'DMs are off, so your DM templates, follow-ups, lead magnet and catch-up messages will never send.',
      fix: { label: 'Allow 5 DMs/day', patch: { max_dms_per_day: 5 } },
    })
  }
  // C10 — no milestone types selected turns catch-up off regardless of the cap.
  if (catchupCap > 0 && !notEmpty(eng.catchup_event_types)) {
    out.push({
      id: 'C10', severity: 'inform', section: 'outreach', anchor: 'catchup_event_types',
      message: 'No milestones are selected, so catch-up is off — the cap above has nothing to spend.',
    })
  }
  // C11 — intended, but worth saying out loud.
  if (eng.connection_targeting_mode === 'auto_queue' && eng.connection_request_mode === 'pre_review') {
    out.push({
      id: 'C11', severity: 'inform', section: 'outreach', anchor: 'connection_request_mode',
      message: 'Targets are sourced automatically but still wait for your approval before anything is sent.',
    })
  }
  // C12 — adjacent authors are only read when sourcing is on.
  if (eng.connection_targeting_mode === 'off' && notEmpty(eng.connection_target_authors)) {
    out.push({
      id: 'C12', severity: 'inform', section: 'outreach', anchor: 'connection_target_authors',
      message: 'Connection targeting is off, so these authors are not being used.',
    })
  }
  // C13 — an ICP floor this high clears almost nobody, and only applies to scraped profiles.
  if ((eng.min_connection_icp_score ?? 55) >= 80) {
    out.push({
      id: 'C13', severity: 'warn', section: 'outreach', anchor: 'min_connection_icp_score',
      message: 'Very few people clear a fit score this high, and it is only applied to profiles LEM has actually scraped — expect very few connection targets.',
      fix: { label: 'Reset to 55', patch: { min_connection_icp_score: 55 } },
    })
  }
  // C14 — "invites" means three different things (F5).
  if (eng.max_invites_per_day === 0 && newsletter?.invite_connections_enabled) {
    out.push({
      id: 'C14', severity: 'inform', section: 'volume', anchor: 'max_invites_per_day',
      message: 'This cap covers connection requests and profile-viewer invites only. Newsletter subscribe invites have their own cap and will still send.',
    })
  }
  // C15 — auto-schedule on, but the gates hold nearly everything anyway.
  const gateDefaults = eng.gate_defaults ?? { authenticity_score_min: 60, post_similarity_max_pct: 55 }
  const strictGates =
    (eng.authenticity_score_min ?? gateDefaults.authenticity_score_min) >= 80 ||
    (eng.post_similarity_max_pct ?? gateDefaults.post_similarity_max_pct) <= 25
  if (user?.auto_schedule_posts && strictGates) {
    out.push({
      id: 'C15', severity: 'warn', section: 'content', anchor: 'authenticity_score_min',
      message: 'Auto-scheduling is on, but with thresholds this strict most drafts will still be held for your review — which looks like auto-scheduling is broken.',
      fix: { label: 'Use the default thresholds', patch: { authenticity_score_min: null, post_similarity_max_pct: null } },
    })
  }
  // C16 — auto-schedule off means the plan waits on you.
  if (user && !user.auto_schedule_posts) {
    out.push({
      id: 'C16', severity: 'inform', section: 'content', anchor: 'auto_schedule_posts',
      message: 'Every generated post waits for your approval in Content → Review. Nothing publishes on its own.',
    })
  }
  // C17 — the inactivity auto-stop overrides every other setting on this page.
  const inactivity = user?.last_login_inactivate_delay
  if (inactivity !== null && inactivity !== undefined && inactivity <= 30) {
    out.push({
      id: 'C17', severity: 'inform', section: 'setup', anchor: 'last_login_inactivate_delay',
      message: `If you do not sign in for ${inactivity} days, ALL automation pauses — every other setting on this page stops applying until you log back in.`,
    })
  }
  // C18 — a newsletter draft that appears the day it publishes leaves no review window.
  if (newsletter?.enabled && newsletter.generate_lead_days === 0) {
    out.push({
      id: 'C18', severity: 'warn', section: 'newsletter', anchor: 'generate_lead_days',
      message: 'Drafts are generated the same day they publish, so you get no window to review or edit one.',
    })
  }
  // C19 — publish hour is resolved in the account timezone, not the browser's.
  if (newsletter?.enabled && timezone && browserTimezone && timezone !== browserTimezone) {
    out.push({
      id: 'C19', severity: 'inform', section: 'newsletter', anchor: 'publish_hour',
      message: `Publish time is resolved in your account timezone (${timezone}), not the ${browserTimezone} your browser is in.`,
    })
  }
  // C20 — excludes always win, so an exclusion that overlaps your own focus kills it silently.
  const overlaps = overlappingExclusions(eng)
  if (overlaps.length > 0) {
    out.push({
      id: 'C20', severity: 'warn', section: 'targeting', anchor: 'exclude_topics',
      message: `Exclusions always win over includes, so ${overlaps.join(', ')} is both steering your content and being filtered out of your feed.`,
    })
  }
  // C21 — a tiny recency window on top of include filters leaves almost no candidates.
  if (includes && (eng.max_post_age_hours ?? 24) <= 4) {
    out.push({
      id: 'C21', severity: 'warn', section: 'targeting', anchor: 'max_post_age_hours',
      message: 'Only posts from the last few hours can match your include filters — that is a very small candidate pool.',
      fix: { label: 'Widen to 24 hours', patch: { max_post_age_hours: 24 } },
    })
  }
  // C22 — body links cost reach, which works against a lead magnet.
  if (!eng.link_in_first_comment && leadMagnet?.enabled) {
    out.push({
      id: 'C22', severity: 'inform', section: 'content', anchor: 'link_in_first_comment',
      message: 'Your lead magnet depends on reach, and links in the post body cost roughly 60-68% of it. Moving links to the first comment usually converts better.',
      fix: { label: 'Put links in the first comment', patch: { link_in_first_comment: true } },
    })
  }
  // C23 — premium video silently degrades with no credits.
  if (eng.default_video_quality !== 'standard' && videoCredits <= 0) {
    out.push({
      id: 'C23', severity: 'inform', section: 'content', anchor: 'default_video_quality',
      message: 'You have no video credits, so auto-generated videos fall back to standard quality until you top up.',
    })
  }
  // C24 — the catch-up cap is clamped per plan, on save AND on downgrade.
  if (catchupCap >= catchupAllowed && catchupAllowed <= 5) {
    out.push({
      id: 'C24', severity: 'inform', section: 'volume', anchor: 'max_catchup_touches_per_day',
      message: 'Your plan allows up to 5 catch-up messages a day; 10/day is included with Professional and Enterprise.',
    })
  }
  // C25 / C26 — reach-cost informers that used to be buried in grey microcopy.
  if (eng.use_hashtags) {
    out.push({
      id: 'C25', severity: 'inform', section: 'voice', anchor: 'use_hashtags',
      message: 'Hashtags no longer expand reach — hashtag-free posts reach roughly 5-10% more people in 2026.',
    })
  }
  if (eng.comment_length === 'short') {
    out.push({
      id: 'C26', severity: 'inform', section: 'voice', anchor: 'comment_length',
      message: 'LinkedIn weights substantive comments (15+ words) about 2.5× higher than one-liners.',
    })
  }
  // C27 — cadence above the 2026 sweet spot. Posting more often reaches fewer people per post, so
  // this is the one place a higher number is the worse setting (issue #621).
  const cadence = eng.posts_per_week ?? 3
  if (cadence > 4) {
    out.push({
      id: 'C27', severity: 'warn', section: 'content', anchor: 'posts_per_week',
      message: `${cadence} posts a week is above the 2-4 that performs best in 2026 — daily posting measures roughly 26% less average reach per post, so more posts usually means fewer readers.`,
      fix: { label: 'Use 3 a week', patch: { posts_per_week: 3 } },
    })
  }
  // C28 — the day allow-list is the harder bound (issue #581). Silently publishing fewer times
  // than the cadence says is exactly the kind of gap a settings screen exists to surface.
  const days = eng.posting_days?.length ? eng.posting_days : [0, 1, 2, 3, 4]
  if (cadence > days.length) {
    out.push({
      id: 'C28', severity: 'warn', section: 'content', anchor: 'posting_days',
      message: `You asked for ${cadence} posts a week but left only ${days.length} day${days.length === 1 ? '' : 's'} switched on, so the plan will publish ${days.length} time${days.length === 1 ? '' : 's'} a week.`,
      // Only offered when the day count is itself a legal cadence — posts_per_week floors at 2, so
      // "match the cadence" to a single day would just swap this warning for a blocking one.
      ...(days.length >= 2
        ? { fix: { label: `Match the cadence to ${days.length} a week`, patch: { posts_per_week: days.length } } }
        : {}),
    })
  }
  // C29 — the company-page cap is bounded by the account-wide invite cap (issue #732), so a bigger
  // number here silently does nothing. Say so rather than letting the two disagree on screen.
  if ((eng.max_company_page_invites_per_day ?? 5) > (eng.max_invites_per_day ?? 0)) {
    out.push({
      id: 'C29', severity: 'inform', section: 'volume', anchor: 'max_company_page_invites_per_day',
      message: `Invites per day (${eng.max_invites_per_day ?? 0}) is the harder ceiling, so company-page invites will stop at that number.`,
    })
  }
  return out
}

export const findingsFor = (findings: Finding[], anchor: string) =>
  findings.filter((f) => f.anchor === anchor)

export const hasBlockingFindings = (findings: Finding[]) =>
  findings.some((f) => f.severity === 'block')

/** Per-section counts for the nav rail badge — blocks and warnings only; informers are not alarms. */
export function sectionAlertCounts(findings: Finding[]): Record<string, number> {
  const counts: Record<string, number> = {}
  for (const f of findings) {
    if (f.severity === 'inform') continue
    counts[f.section] = (counts[f.section] ?? 0) + 1
  }
  return counts
}
