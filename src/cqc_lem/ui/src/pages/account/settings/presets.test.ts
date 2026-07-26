import { describe, expect, it } from 'vitest'
import type { EngPrefs } from '../types'
import { PRESETS, detectPreset, getPreset, presetValues } from './presets'

// A minimal EngPrefs; each test overrides only what it cares about.
const base = (over: Partial<EngPrefs> = {}): EngPrefs => ({
  tone: null, comment_length: 'medium', comment_style: null, use_emojis: true, use_hashtags: false,
  include_topics: [], exclude_topics: [], include_keywords: [], exclude_keywords: [],
  include_authors: [], exclude_authors: [], post_types: [], default_buyer_stage: null,
  focus_topics: [], business_goals: null, personal_goals: null,
  authenticity_score_min: null, post_similarity_max_pct: null,
  min_reactions: null, max_post_age_hours: 24, reply_to_own_comments: true,
  max_comments_per_day: 20, max_dms_per_day: 20, max_invites_per_day: 10,
  connection_request_mode: 'auto_approve', connection_targeting_mode: 'suggest',
  connection_target_authors: [], min_connection_icp_score: 55,
  default_video_quality: 'standard', reply_check_mode: 'event', reply_sweeps_per_day: 2,
  reply_max_post_age_days: 2, feed_fallback_when_empty: true, link_in_first_comment: true,
  max_catchup_touches_per_day: 5, catchup_touch_mode: 'pre_review',
  catchup_event_types: ['job_change', 'promotion'], catchup_message_source: 'linkedin',
  posts_per_week: 3,
  ...over,
})

describe('presets', () => {
  it('never exceeds the shipped per-user ceilings', () => {
    for (const p of PRESETS) {
      expect(p.values.max_comments_per_day).toBeLessThanOrEqual(20)
      expect(p.values.max_dms_per_day).toBeLessThanOrEqual(20)
      expect(p.values.max_invites_per_day).toBeLessThanOrEqual(10)
    }
  })

  it('mirrors the signed-off outbound ramp (P0/P1/P2)', () => {
    expect(getPreset('conservative').values.max_comments_per_day).toBe(8)
    expect(getPreset('balanced').values.max_comments_per_day).toBe(15)
    expect(getPreset('aggressive').values.max_comments_per_day).toBe(20)
    expect(getPreset('conservative').values.connection_request_mode).toBe('pre_review')
  })

  it('clamps the catch-up cap to what the plan allows', () => {
    expect(presetValues('aggressive', 5).max_catchup_touches_per_day).toBe(5)
    expect(presetValues('aggressive', 10).max_catchup_touches_per_day).toBe(10)
  })

  it('detects an exact match', () => {
    const eng = base(presetValues('balanced', 5))
    expect(detectPreset(eng, 5)).toBe('balanced')
  })

  it('is never sticky — changing one owned value flips to Custom', () => {
    const eng = base({ ...presetValues('balanced', 5), max_dms_per_day: 11 })
    expect(detectPreset(eng, 5)).toBeNull()
  })

  it('ignores values a preset does not own', () => {
    const eng = base({ ...presetValues('balanced', 5), tone: 'punchy', min_reactions: 40 })
    expect(detectPreset(eng, 5)).toBe('balanced')
  })

  it('reports Custom for the shipped defaults, so existing users are never shown a preset', () => {
    expect(detectPreset(base(), 5)).toBeNull()
  })
})
