import { describe, expect, it, vi, afterEach, beforeEach } from 'vitest'
import { parseSurvey, surveyEventProps, surveyResponseProps, SURVEY_NAMES } from './usePostHogSurvey'
import type { ActiveSurvey } from '../utils/analytics'

// The pure half of the headless survey renderer (issue #653): which survey we agree to draw, and
// the `survey sent` payload PostHog's Surveys product reads. Both are contracts with something
// outside this repo, so they get asserted rather than eyeballed.

afterEach(() => vi.restoreAllMocks())

function survey(overrides: Record<string, unknown> = {}): ActiveSurvey {
  return {
    id: '0199-nps',
    name: SURVEY_NAMES.nps,
    type: 'api',
    questions: [
      {
        type: 'rating',
        id: 'q-rating',
        question: 'How likely are you to recommend LEM to a colleague?',
        display: 'number',
        scale: 10,
        lowerBoundLabel: 'Not at all likely',
        upperBoundLabel: 'Extremely likely',
      },
      { type: 'open', id: 'q-open', question: "What's the main reason for your score?" },
    ],
    current_iteration: null,
    current_iteration_start_date: null,
    ...overrides,
  } as unknown as ActiveSurvey
}

describe('parseSurvey', () => {
  it('reads the NPS survey as a 0-10 band', () => {
    const parsed = parseSurvey([survey()])
    expect(parsed?.kind).toBe('nps')
    expect(parsed?.rating.min).toBe(0)
    expect(parsed?.rating.max).toBe(10)
    expect(parsed?.rating.lowerBoundLabel).toBe('Not at all likely')
    expect(parsed?.open?.question).toBe("What's the main reason for your score?")
  })

  it('reads the CSAT survey as a 1-5 scale', () => {
    // PostHog's 10-point rating IS the NPS band and starts at 0; every other scale starts at 1.
    const csat = survey({
      id: '0199-csat',
      name: SURVEY_NAMES.csat,
      questions: [
        { type: 'rating', id: 'r', question: 'How happy are you?', display: 'number', scale: 5 },
      ],
    })
    const parsed = parseSurvey([csat])
    expect(parsed?.kind).toBe('csat')
    expect(parsed?.rating.min).toBe(1)
    expect(parsed?.rating.max).toBe(5)
    expect(parsed?.open).toBeNull()
  })

  it('ignores a survey LEM does not own', () => {
    // Someone creating a marketing survey in the PostHog UI must not have it drawn by a component
    // that only knows how to render a rating plus an optional "why".
    expect(parseSurvey([survey({ name: 'Pricing research' })])).toBeNull()
  })

  it('skips one of ours that has no rating question', () => {
    const broken = survey({ questions: [{ type: 'open', id: 'o', question: 'Thoughts?' }] })
    expect(parseSurvey([broken])).toBeNull()
  })

  it('picks the first LEM survey out of a mixed list', () => {
    const parsed = parseSurvey([survey({ name: 'Pricing research' }), survey()])
    expect(parsed?.survey.id).toBe('0199-nps')
  })

  it('is empty for an empty eligible set', () => {
    expect(parseSurvey([])).toBeNull()
  })
})

describe('survey event properties', () => {
  it('carries the survey identity on every event so shown and sent line up', () => {
    expect(surveyEventProps(survey())).toEqual({
      $survey_id: '0199-nps',
      $survey_name: SURVEY_NAMES.nps,
      $survey_iteration: null,
      $survey_iteration_start_date: null,
    })
  })

  it('reports an iteration when the survey is on one', () => {
    const props = surveyEventProps(
      survey({ current_iteration: 2, current_iteration_start_date: '2026-07-01T00:00:00Z' })
    )
    expect(props.$survey_iteration).toBe(2)
    expect(props.$survey_iteration_start_date).toBe('2026-07-01T00:00:00Z')
  })

  it('emits the response in PostHog own shape — by index AND by question id', () => {
    const props = surveyResponseProps(survey(), [9, 'It sounds like me'])
    expect(props.$survey_response).toBe(9)
    expect(props.$survey_response_1).toBe('It sounds like me')
    expect(props['$survey_response_q-rating']).toBe(9)
    expect(props['$survey_response_q-open']).toBe('It sounds like me')
    expect(props.$survey_completed).toBe(true)
    expect(props.$survey_questions).toEqual([
      {
        id: 'q-rating',
        question: 'How likely are you to recommend LEM to a colleague?',
        response: 9,
      },
      { id: 'q-open', question: "What's the main reason for your score?", response: 'It sounds like me' },
    ])
  })

  it('leaves a skipped open question out of the response keys but keeps it in the questions list', () => {
    const props = surveyResponseProps(survey(), [10, null])
    expect(props.$survey_response).toBe(10)
    expect('$survey_response_1' in props).toBe(false)
    expect((props.$survey_questions as Array<{ response: unknown }>)[1].response).toBeNull()
  })

  it('does not treat a zero score as a skipped answer', () => {
    // A detractor scoring 0 is the single most important response there is; a falsy check here
    // would silently drop it.
    const props = surveyResponseProps(survey(), [0, null])
    expect(props.$survey_response).toBe(0)
  })
})

describe('recordPostApproval', () => {
  beforeEach(() => vi.resetModules())

  it('notifies survey subscribers so the CSAT can be re-checked at the approval', async () => {
    const analytics = await import('../utils/analytics')
    const seen: number[] = []
    const off = analytics.onPostApproval(() => seen.push(1))
    analytics.recordPostApproval({ post_id: 1 })
    analytics.recordPostApproval({ post_id: 2 })
    off()
    analytics.recordPostApproval({ post_id: 3 })
    expect(seen).toHaveLength(2)
  })

  it('survives a listener that throws — analytics must never break an approval', async () => {
    const analytics = await import('../utils/analytics')
    const off = analytics.onPostApproval(() => {
      throw new Error('boom')
    })
    expect(() => analytics.recordPostApproval()).not.toThrow()
    off()
  })
})
