import { describe, expect, it } from 'vitest'
import { emptyRunExplanation, isGenerationRunning } from './generationStatus'
import type { GenerationState, GenerationStatus } from './generationStatus'

const status = (over: Partial<GenerationStatus> = {}): GenerationStatus => ({
  state: 'done',
  total: 0,
  completed: 0,
  failed: 0,
  post_ids: [],
  ready_post_ids: [],
  failed_post_ids: [],
  started_at: '2026-07-27T13:52:00+00:00',
  finished_at: '2026-07-27T13:52:59+00:00',
  ...over,
})

const TZ = 'America/New_York'

describe('isGenerationRunning', () => {
  it('is true while queued or in progress', () => {
    const running: GenerationState[] = ['queued', 'in_progress']
    running.forEach((state) => expect(isGenerationRunning(status({ state }))).toBe(true))
  })

  it('is false once the run is finished', () => {
    const finished: GenerationState[] = ['done', 'failed']
    finished.forEach((state) => expect(isGenerationRunning(status({ state }))).toBe(false))
  })

  it('is false with nothing tracked', () => {
    expect(isGenerationRunning(null)).toBe(false)
  })
})

describe('emptyRunExplanation', () => {
  it('names the next planned slot when nothing is due yet (issue #719)', () => {
    const e = emptyRunExplanation(
      status({ reason: 'no_planned_slots', reason_detail: { next_planned_at: '2026-08-03T13:30:00+00:00', buffer_days: 5 } }),
      TZ,
    )
    expect(e?.headline).toContain('Nothing to generate right now')
    expect(e?.detail).toContain('Aug 3')
    expect(e?.detail).toContain('5 days before each planned slot')
  })

  it('renders the slot date in the USER timezone, not the browser one', () => {
    // 03:30 UTC on Aug 4 is still Aug 3 in New York — a naive read would show the wrong day.
    const e = emptyRunExplanation(
      status({ reason: 'no_planned_slots', reason_detail: { next_planned_at: '2026-08-04T03:30:00+00:00' } }),
      TZ,
    )
    expect(e?.detail).toContain('Aug 3')
  })

  it('falls back to a plan-level explanation with no upcoming slot at all', () => {
    const e = emptyRunExplanation(status({ reason: 'no_planned_slots', reason_detail: {} }), TZ)
    expect(e?.detail).toContain('no upcoming posts left to generate')
    expect(e?.detail).not.toContain('undefined')
  })

  it('reports what is already waiting when the buffer is full', () => {
    const e = emptyRunExplanation(
      status({ reason: 'buffer_full', reason_detail: { ready_count: 5, buffer_max: 5, buffer_days: 5, next_planned_at: '2026-08-03T13:30:00+00:00' } }),
      TZ,
    )
    expect(e?.detail).toContain('5 posts are already generated')
    expect(e?.detail).toContain('Aug 3')
  })

  it('says post, singular, for a single ready post', () => {
    const e = emptyRunExplanation(
      status({ reason: 'buffer_full', reason_detail: { ready_count: 1 } }),
      TZ,
    )
    expect(e?.detail).toContain('1 post is already generated')
  })

  it('explains a run that lost the single-flight lock', () => {
    const e = emptyRunExplanation(status({ reason: 'already_running' }), TZ)
    expect(e?.headline).toContain('already in progress')
  })

  it('stays out of the way when posts were generated', () => {
    expect(
      emptyRunExplanation(status({ total: 2, completed: 2, reason: 'buffer_full' }), TZ),
    ).toBeNull()
  })

  it('stays out of the way for a pre-#719 run that carries no reason', () => {
    expect(emptyRunExplanation(status(), TZ)).toBeNull()
  })

  it('stays out of the way while the run is still going', () => {
    expect(
      emptyRunExplanation(status({ state: 'in_progress', reason: 'buffer_full' }), TZ),
    ).toBeNull()
  })
})
