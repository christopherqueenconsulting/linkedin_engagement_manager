import { describe, expect, it } from 'vitest'
import type { EngagementTarget, EngPrefs, NewsletterSettings, UserPrefs } from '../types'
import {
  blockingIssues, evaluateConflicts, hasBlockingFindings, overlappingExclusions,
  sectionAlertCounts, type ConflictContext,
} from './conflicts'

const eng = (over: Partial<EngPrefs> = {}): EngPrefs => ({
  tone: null, comment_length: 'medium', comment_style: null, use_emojis: true, use_hashtags: false,
  include_topics: [], exclude_topics: [], include_keywords: [], exclude_keywords: [],
  include_authors: [], exclude_authors: [], post_types: [], default_buyer_stage: null,
  focus_topics: [], business_goals: null, personal_goals: null,
  authenticity_score_min: null, post_similarity_max_pct: null,
  min_reactions: null, max_post_age_hours: 24, reply_to_own_comments: true,
  max_comments_per_day: 20, max_dms_per_day: 20, max_invites_per_day: 10,
  max_company_page_invites_per_day: 5,
  connection_request_mode: 'auto_approve', connection_targeting_mode: 'suggest',
  connection_target_authors: [], min_connection_icp_score: 55,
  default_video_quality: 'standard', reply_check_mode: 'event', reply_sweeps_per_day: 2,
  reply_max_post_age_days: 2, feed_fallback_when_empty: true, link_in_first_comment: true,
  max_catchup_touches_per_day: 5, catchup_touch_mode: 'pre_review',
  catchup_event_types: ['job_change', 'promotion'], catchup_message_source: 'linkedin',
  min_catchup_contact_interval_days: 14, max_catchup_touches_per_contact_days: 2,
  posts_per_week: 3, posting_days: [0, 1, 2, 3, 4],
  text_post_images: true, roster_auto_follow: false, max_follows_per_day: 3,
  roster_auto_connect: false, profile_viewer_dm_auto_send: false,
  gmail_forward_confirmation: { confirmed: true },
  // Read-only context the endpoint always returns alongside the row (issue #1446).
  reply_inbound_address: null, feed_reach: null,
  has_saved_preferences: true, max_catchup_touches_allowed: 5,
  catchup_contact_interval_bounds: { min_days: 0, max_days: 90 },
  catchup_per_contact_cap_bounds: { min: 1, max: 10 },
  gate_defaults: { authenticity_score_min: 60, post_similarity_max_pct: 55 },
  ...over,
})

const user = (over: Partial<UserPrefs> = {}): UserPrefs => ({
  last_login_inactivate_delay: 90, auto_schedule_posts: true, content_language: null,
  content_buffer_days: 5, content_buffer_max_posts: 5, ...over,
})

// `newsletter_url` / `last_published_at` are written by the publish run, not by the settings PUT —
// they are on the wire either way, which is why the generated type requires them here.
const newsletter = (over: Partial<NewsletterSettings> = {}): NewsletterSettings => ({
  enabled: true, title: 'Brief', topic: null, cadence: 'weekly', align_with_blog: true,
  newsletter_url: null, last_published_at: null,
  publish_day: 1, publish_hour: 9, generate_lead_days: 3, max_queued_drafts: 1,
  invite_connections_enabled: false, max_invites_per_run: 50, cover_image_auto: false, ...over,
})

const ctx = (over: Partial<ConflictContext> = {}): ConflictContext => ({
  eng: eng(), user: user(), newsletter: null, leadMagnet: null, videoCredits: 5,
  timezone: 'America/New_York', browserTimezone: 'America/New_York', catchupAllowed: 5, ...over,
})

const ids = (c: ConflictContext) => evaluateConflicts(c).map((f) => f.id)
const find = (c: ConflictContext, id: string) => evaluateConflicts(c).find((f) => f.id === id)

describe('conflict matrix', () => {
  it('C1 cites the user\'s own last feed scan', () => {
    const f = find(ctx({ eng: eng({
      include_topics: ['AI'],
      feed_reach: { examined: 40, passed_filters: 12, matched_topics: 0, commented: 0, fallback_used: false },
    }) }), 'C1')
    expect(f?.severity).toBe('warn')
    expect(f?.message).toContain('40')
  })

  it('C1 stays quiet when the last scan matched something', () => {
    expect(ids(ctx({ eng: eng({
      include_topics: ['AI'],
      feed_reach: { examined: 40, passed_filters: 12, matched_topics: 3, commented: 2, fallback_used: false },
    }) }))).not.toContain('C1')
  })

  it('C2 warns when includes are set with the fallback off, and offers a one-click fix', () => {
    const f = find(ctx({ eng: eng({ include_topics: ['AI'], feed_fallback_when_empty: false }) }), 'C2')
    expect(f?.severity).toBe('warn')
    expect(f?.fix?.patch).toEqual({ feed_fallback_when_empty: true })
  })

  it('C3 informs that the fallback is a no-op without include filters', () => {
    expect(find(ctx(), 'C3')?.severity).toBe('inform')
    expect(ids(ctx({ eng: eng({ include_keywords: ['ops'] }) }))).not.toContain('C3')
  })

  it('C4 warns that targeting is dead config when commenting is off', () => {
    expect(find(ctx({ eng: eng({ max_comments_per_day: 0 }) }), 'C4')?.severity).toBe('warn')
  })

  it('C5 informs that the reply cadence is ignored outside scheduled mode', () => {
    expect(find(ctx({ eng: eng({ reply_check_mode: 'off', reply_sweeps_per_day: 6 }) }), 'C5')?.severity).toBe('inform')
    expect(ids(ctx({ eng: eng({ reply_check_mode: 'scheduled', reply_sweeps_per_day: 6 }) }))).not.toContain('C5')
  })

  it('C6 warns when event mode has no confirmed forwarding', () => {
    expect(find(ctx({ eng: eng({ gmail_forward_confirmation: { confirmed: false } }) }), 'C6')?.severity).toBe('warn')
    expect(ids(ctx())).not.toContain('C6')
  })

  it('C6 sends a user who has not started to the 3-step setup', () => {
    const f = find(ctx({ eng: eng({ gmail_forward_confirmation: null }) }), 'C6')
    expect(f?.message).toContain('3-step setup')
  })

  it('C6 names the outstanding step once Gmail has asked us to verify (#813)', () => {
    // A pending record means the address WAS added — repeating "start the 3-step setup" is what
    // made a half-done setup read as a total failure.
    const f = find(ctx({ eng: eng({ gmail_forward_confirmation: { confirmed: false, code: '123456789' } }) }), 'C6')
    expect(f?.message).not.toContain('3-step setup')
    expect(f?.message).toContain('no forwarded LinkedIn email has arrived yet')
  })

  it('C6 clears once forwarding is confirmed by an arriving email', () => {
    expect(ids(ctx({ eng: eng({ gmail_forward_confirmation: { confirmed: true, source: 'forwarded_email' } }) })))
      .not.toContain('C6')
  })

  it('C7 prices the 429 risk of scheduled sweeps', () => {
    const f = find(ctx({ eng: eng({ reply_check_mode: 'scheduled', reply_sweeps_per_day: 8 }) }), 'C7')
    expect(f?.message).toContain('8')
    expect(f?.fix?.patch).toEqual({ reply_check_mode: 'event' })
  })

  it('C8 warns when catch-up can eat the whole DM budget', () => {
    const f = find(ctx({ eng: eng({ max_catchup_touches_per_day: 5, max_dms_per_day: 5 }) }), 'C8')
    expect(f?.severity).toBe('warn')
    expect(f?.fix?.patch).toEqual({ max_dms_per_day: 10 })
  })

  it('C9 warns that nothing sends with DMs off', () => {
    expect(find(ctx({ eng: eng({ max_dms_per_day: 0 }) }), 'C9')?.severity).toBe('warn')
  })

  it('C10 informs that no milestones means catch-up is off', () => {
    expect(find(ctx({ eng: eng({ catchup_event_types: [] }) }), 'C10')?.severity).toBe('inform')
  })

  it('C11 explains auto-queue plus pre-review', () => {
    expect(find(ctx({ eng: eng({
      connection_targeting_mode: 'auto_queue', connection_request_mode: 'pre_review',
    }) }), 'C11')?.severity).toBe('inform')
  })

  it('C12 informs that adjacent authors are unused while targeting is off', () => {
    expect(find(ctx({ eng: eng({
      connection_targeting_mode: 'off', connection_target_authors: ['https://x'],
    }) }), 'C12')?.severity).toBe('inform')
  })

  it('C13 warns about an unreachable ICP floor', () => {
    expect(find(ctx({ eng: eng({ min_connection_icp_score: 85 }) }), 'C13')?.fix?.patch)
      .toEqual({ min_connection_icp_score: 55 })
    expect(ids(ctx())).not.toContain('C13')
  })

  it('C14 explains that newsletter invites have their own cap', () => {
    expect(find(ctx({
      eng: eng({ max_invites_per_day: 0 }),
      newsletter: newsletter({ invite_connections_enabled: true }),
    }), 'C14')?.severity).toBe('inform')
  })

  it('C15 warns that strict gates defeat auto-scheduling', () => {
    const f = find(ctx({ eng: eng({ authenticity_score_min: 90 }), user: user({ auto_schedule_posts: true }) }), 'C15')
    expect(f?.severity).toBe('warn')
    expect(f?.fix?.patch).toEqual({ authenticity_score_min: null, post_similarity_max_pct: null })
    expect(ids(ctx({ eng: eng({ post_similarity_max_pct: 20 }) }))).toContain('C15')
  })

  it('C16 informs that nothing publishes while auto-schedule is off', () => {
    expect(find(ctx({ user: user({ auto_schedule_posts: false }) }), 'C16')?.severity).toBe('inform')
  })

  it('C17 flags a short inactivity auto-stop as overriding everything', () => {
    expect(find(ctx({ user: user({ last_login_inactivate_delay: 30 }) }), 'C17')?.severity).toBe('inform')
    expect(ids(ctx({ user: user({ last_login_inactivate_delay: null }) }))).not.toContain('C17')
  })

  it('C18 warns about a newsletter with no review window', () => {
    expect(find(ctx({ newsletter: newsletter({ generate_lead_days: 0 }) }), 'C18')?.severity).toBe('warn')
  })

  it('C19 informs when the account timezone is not the browser one', () => {
    expect(find(ctx({ newsletter: newsletter(), browserTimezone: 'Europe/London' }), 'C19')?.severity).toBe('inform')
    expect(ids(ctx({ newsletter: newsletter() }))).not.toContain('C19')
  })

  it('C20 names the terms that are both steered toward and excluded', () => {
    const f = find(ctx({ eng: eng({ focus_topics: ['AI adoption'], exclude_topics: ['ai adoption'] }) }), 'C20')
    expect(f?.severity).toBe('warn')
    expect(f?.message).toContain('ai adoption')
  })

  it('C21 warns about a tiny recency window on top of include filters', () => {
    expect(find(ctx({ eng: eng({ include_topics: ['AI'], max_post_age_hours: 2 }) }), 'C21')?.fix?.patch)
      .toEqual({ max_post_age_hours: 24 })
    expect(ids(ctx({ eng: eng({ max_post_age_hours: 2 }) }))).not.toContain('C21')
  })

  it('C22 links body links to the lead magnet\'s reach', () => {
    expect(find(ctx({
      eng: eng({ link_in_first_comment: false }),
      leadMagnet: { enabled: true, keyword: 'guide', message: null },
    }), 'C22')?.severity).toBe('inform')
  })

  it('C23 explains the silent downgrade with no video credits', () => {
    expect(find(ctx({ eng: eng({ default_video_quality: 'premium' }), videoCredits: 0 }), 'C23')?.severity).toBe('inform')
    expect(ids(ctx({ eng: eng({ default_video_quality: 'premium' }), videoCredits: 3 }))).not.toContain('C23')
  })

  it('C24 explains the per-plan catch-up clamp', () => {
    expect(find(ctx(), 'C24')?.severity).toBe('inform')
    expect(ids(ctx({ eng: eng({ max_catchup_touches_per_day: 5 }), catchupAllowed: 10 }))).not.toContain('C24')
  })

  it('C25 and C26 keep the reach-cost microcopy visible', () => {
    expect(find(ctx({ eng: eng({ use_hashtags: true }) }), 'C25')?.severity).toBe('inform')
    expect(find(ctx({ eng: eng({ comment_length: 'short' }) }), 'C26')?.severity).toBe('inform')
  })

  it('C27 warns once the cadence goes past the 2026 sweet spot', () => {
    expect(find(ctx({ eng: eng({ posts_per_week: 3 }) }), 'C27')).toBeUndefined()
    expect(find(ctx({ eng: eng({ posts_per_week: 4 }) }), 'C27')).toBeUndefined()
    const daily = find(ctx({ eng: eng({ posts_per_week: 7 }) }), 'C27')
    expect(daily?.severity).toBe('warn')
    expect(daily?.anchor).toBe('posts_per_week')
    expect(daily?.message).toMatch(/26%/)
    expect(daily?.fix?.patch).toEqual({ posts_per_week: 3 })
  })

  it('C28 warns when the cadence asks for more days than are switched on', () => {
    expect(find(ctx({ eng: eng({ posts_per_week: 3, posting_days: [0, 1, 2, 3, 4] }) }), 'C28'))
      .toBeUndefined()
    const capped = find(ctx({ eng: eng({ posts_per_week: 4, posting_days: [1, 3] }) }), 'C28')
    expect(capped?.severity).toBe('warn')
    expect(capped?.anchor).toBe('posting_days')
    expect(capped?.fix?.patch).toEqual({ posts_per_week: 2 })
    // A single day can't become a legal cadence (the floor is 2), so no one-click fix is offered.
    expect(find(ctx({ eng: eng({ posts_per_week: 3, posting_days: [1] }) }), 'C28')?.fix)
      .toBeUndefined()
  })

  it('C31 says which switched-on days the cadence actually fills (issue #1526)', () => {
    // Mon-Fri at 3/week is the shipped default, so it informs rather than alarms.
    const untouched = find(ctx({ eng: eng({ posts_per_week: 3, posting_days: [0, 1, 2, 3, 4] }) }), 'C31')
    expect(untouched?.severity).toBe('inform')
    // Days the user deliberately switched on and never gets a post on are a real gap.
    const allSeven = find(ctx({ eng: eng({ posts_per_week: 3, posting_days: [0, 1, 2, 3, 4, 5, 6] }) }), 'C31')
    expect(allSeven?.severity).toBe('warn')
    expect(allSeven?.anchor).toBe('posts_per_week')
    expect(allSeven?.message).toMatch(/Tue, Wed, Thu/)
    expect(allSeven?.fix?.patch).toEqual({ posts_per_week: 7 })
    // Cadence matching the day count leaves nothing unused.
    expect(find(ctx({ eng: eng({ posts_per_week: 5, posting_days: [0, 1, 2, 3, 4] }) }), 'C31'))
      .toBeUndefined()
  })

  it('never warns about a clean, default configuration', () => {
    const warnings = evaluateConflicts(ctx()).filter((f) => f.severity !== 'inform')
    expect(warnings).toEqual([])
  })
})

describe('blocking validation', () => {
  it('blocks an over-long tone rather than losing the whole row', () => {
    const found = blockingIssues(eng({ tone: 'x'.repeat(256) }))
    expect(found.map((f) => f.id)).toEqual(['B:tone'])
    expect(hasBlockingFindings(found)).toBe(true)
  })

  it('blocks out-of-band numerics', () => {
    const found = blockingIssues(eng({
      min_reactions: -1, reply_sweeps_per_day: 99, min_connection_icp_score: 500,
      post_similarity_max_pct: 5,
    })).map((f) => f.id)
    expect(found).toContain('B:min_reactions')
    expect(found).toContain('B:reply_sweeps_per_day')
    expect(found).toContain('B:min_connection_icp_score')
    expect(found).toContain('B:post_similarity_max_pct')
  })

  it('blocks a cadence outside 2-7 a week', () => {
    expect(blockingIssues(eng({ posts_per_week: 1 })).map((f) => f.id)).toContain('B:posts_per_week')
    expect(blockingIssues(eng({ posts_per_week: 8 })).map((f) => f.id)).toContain('B:posts_per_week')
  })

  it('accepts a null threshold as "use the default"', () => {
    expect(blockingIssues(eng({ authenticity_score_min: null, post_similarity_max_pct: null }))).toEqual([])
  })

  it('passes a clean object', () => {
    expect(blockingIssues(eng())).toEqual([])
  })
})

describe('C30 — a roster that mostly blocks commenting (issue #962)', () => {
  const target = (over: Partial<EngagementTarget> = {}): EngagementTarget => ({
    profile_url: `https://www.linkedin.com/in/a${Math.random()}`, name: null, category: 'peer',
    max_comments_per_week: 2, active: true, source: 'user', ...over,
  })
  const roster = (blocked: number, total: number) => [
    ...Array.from({ length: blocked }, () => target({ comment_blocked_streak: 2 })),
    ...Array.from({ length: total - blocked }, () => target()),
  ]

  it('warns once most of the active roster is unreachable', () => {
    const f = find(ctx({ roster: roster(4, 8) }), 'C30')
    expect(f?.severity).toBe('warn')
    expect(f?.section).toBe('targeting')
    expect(f?.message).toContain('4 of your 8')
  })

  it('stays quiet on a healthy roster', () => {
    expect(ids(ctx({ roster: roster(1, 8) }))).not.toContain('C30')
  })

  it('does not fire on a tiny roster, where one blocked account is not a pattern', () => {
    expect(ids(ctx({ roster: roster(2, 3) }))).not.toContain('C30')
  })

  it('counts only active rows — a paused account is not a blocked one', () => {
    const rows = [
      ...Array.from({ length: 4 }, () => target({ comment_blocked_streak: 2, active: false })),
      ...Array.from({ length: 5 }, () => target()),
    ]
    expect(ids(ctx({ roster: rows }))).not.toContain('C30')
  })

  it('is absent, not thrown, when the roster has not loaded', () => {
    expect(ids(ctx({ roster: null }))).not.toContain('C30')
    expect(ids(ctx())).not.toContain('C30')
  })
})

describe('helpers', () => {
  it('overlappingExclusions is case- and whitespace-insensitive', () => {
    expect(overlappingExclusions(eng({ focus_topics: [' AI '], exclude_keywords: ['ai'] }))).toEqual(['ai'])
  })

  it('sectionAlertCounts counts warnings and blocks, not informers', () => {
    const counts = sectionAlertCounts([
      { id: 'a', severity: 'warn', section: 'volume', anchor: 'x', message: '' },
      { id: 'b', severity: 'block', section: 'volume', anchor: 'y', message: '' },
      { id: 'c', severity: 'inform', section: 'voice', anchor: 'z', message: '' },
    ])
    expect(counts).toEqual({ volume: 2 })
  })
})
