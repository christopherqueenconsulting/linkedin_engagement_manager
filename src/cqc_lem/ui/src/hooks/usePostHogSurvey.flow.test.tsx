import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest'
import { act, cleanup, renderHook, waitFor } from '@testing-library/react'
import type { ActiveSurvey } from '../utils/analytics'

// The stateful half of the headless survey renderer (issue #653): one `survey shown` per ask, one
// `survey sent` per answer even across a retry, and never a `dismissed` for a survey that was
// actually completed — a survey product where those double up reports nonsense.

const surveys: ActiveSurvey[] = []
const capture = vi.fn()
const markSurveySeen = vi.fn()
const post = vi.fn()
let flagEnabled = true
let approvalListener: (() => void) | null = null

vi.mock('../utils/analytics', () => ({
  SURVEY_EVENTS: { shown: 'survey shown', sent: 'survey sent', dismissed: 'survey dismissed' },
  analyticsEnabled: () => true,
  capture: (...args: unknown[]) => capture(...args),
  markSurveySeen: (...args: unknown[]) => markSurveySeen(...args),
  activeMatchingSurveys: (cb: (s: ActiveSurvey[]) => void) => cb(surveys),
  onPostApproval: (listener: () => void) => {
    approvalListener = listener
    return () => {
      approvalListener = null
    }
  },
}))
vi.mock('../api/client', () => ({ default: { post: (...args: unknown[]) => post(...args) } }))
vi.mock('../contexts/AuthContext', () => ({ useAuth: () => ({ sessionToken: 'tok' }) }))
vi.mock('./useFeatureFlags', () => ({
  FLAGS: { posthogSurveys: 'posthog-surveys-enabled' },
  useFeatureFlag: () => flagEnabled,
}))

const { SURVEY_NAMES, usePostHogSurvey } = await import('./usePostHogSurvey')

function nps(): ActiveSurvey {
  return {
    id: '0199-nps',
    name: SURVEY_NAMES.nps,
    type: 'api',
    questions: [
      {
        type: 'rating',
        id: 'q-rating',
        question: 'How likely are you to recommend LEM?',
        display: 'number',
        scale: 10,
        lowerBoundLabel: 'Not likely',
        upperBoundLabel: 'Very likely',
      },
      { type: 'open', id: 'q-open', question: 'Why?' },
    ],
    current_iteration: null,
    current_iteration_start_date: null,
  } as unknown as ActiveSurvey
}

function events(name: string) {
  return capture.mock.calls.filter((call) => call[0] === name)
}

beforeEach(() => {
  surveys.length = 0
  surveys.push(nps())
  capture.mockReset()
  markSurveySeen.mockReset()
  post.mockReset()
  post.mockResolvedValue({ data: { detail: {} } })
  flagEnabled = true
})
afterEach(cleanup)

describe('usePostHogSurvey', () => {
  it('shows the eligible survey once and records the ask with posthog-js', async () => {
    const { result } = renderHook(() => usePostHogSurvey())
    await waitFor(() => expect(result.current.parsed?.kind).toBe('nps'))
    expect(events('survey shown')).toHaveLength(1)
    // Without this, posthog's own seen/wait-period checks never advance and the 30-day throttle
    // silently stops working — we render, so the SDK never sees the ask itself.
    expect(markSurveySeen).toHaveBeenCalledWith('0199-nps', null)
  })

  it('claims nothing while paused, then asks once it is allowed to draw', async () => {
    // Paused means a bespoke ask owns the screen (or we do not know yet). `survey shown` and
    // markSurveySeen are claims the user SAW it — emitting either here would understate the
    // response rate and spend the 30-day wait period on an ask nobody ever rendered.
    const { result, rerender } = renderHook(({ paused }) => usePostHogSurvey(paused), {
      initialProps: { paused: true },
    })
    await waitFor(() => expect(result.current.parsed).toBeNull())
    expect(capture).not.toHaveBeenCalled()
    expect(markSurveySeen).not.toHaveBeenCalled()

    rerender({ paused: false })
    await waitFor(() => expect(result.current.parsed?.kind).toBe('nps'))
    expect(events('survey shown')).toHaveLength(1)
    expect(markSurveySeen).toHaveBeenCalledTimes(1)
  })

  it('shows nothing while the flag is off', async () => {
    flagEnabled = false
    const { result } = renderHook(() => usePostHogSurvey())
    await waitFor(() => expect(result.current.parsed).toBeNull())
    expect(capture).not.toHaveBeenCalled()
  })

  it('sends the answer to PostHog and to the feedback loop', async () => {
    const { result } = renderHook(() => usePostHogSurvey())
    await waitFor(() => expect(result.current.parsed).not.toBeNull())

    await act(async () => {
      await result.current.submit(3, '  The comments read like a bot  ')
    })

    const [sent] = events('survey sent')
    expect(sent[1].$survey_id).toBe('0199-nps')
    expect(sent[1].$survey_response).toBe(3)
    expect(post).toHaveBeenCalledWith('/survey/posthog', {
      session_token: 'tok',
      kind: 'nps',
      score: 3,
      comment: 'The comments read like a bot',
      survey_id: '0199-nps',
      survey_name: SURVEY_NAMES.nps,
    })
  })

  it('emits ONE survey sent even when the API call fails and the user retries', async () => {
    post.mockRejectedValueOnce(new Error('502'))
    const { result } = renderHook(() => usePostHogSurvey())
    await waitFor(() => expect(result.current.parsed).not.toBeNull())

    await act(async () => {
      await expect(result.current.submit(9, '')).rejects.toThrow('502')
    })
    await act(async () => {
      await result.current.submit(9, '')
    })

    expect(events('survey sent')).toHaveLength(1)
    expect(post).toHaveBeenCalledTimes(2)
  })

  it('does not record a dismissal for a survey that was answered', async () => {
    const { result } = renderHook(() => usePostHogSurvey())
    await waitFor(() => expect(result.current.parsed).not.toBeNull())

    await act(async () => {
      await result.current.submit(10, '')
    })
    act(() => result.current.dismiss())

    expect(events('survey dismissed')).toHaveLength(0)
    expect(result.current.parsed).toBeNull()
  })

  it('records a dismissal when the user closes without answering', async () => {
    const { result } = renderHook(() => usePostHogSurvey())
    await waitFor(() => expect(result.current.parsed).not.toBeNull())

    act(() => result.current.dismiss())

    expect(events('survey dismissed')).toHaveLength(1)
    expect(result.current.parsed).toBeNull()
  })

  it('re-checks eligibility on an approval without re-showing the survey on screen', async () => {
    const { result } = renderHook(() => usePostHogSurvey())
    await waitFor(() => expect(result.current.parsed).not.toBeNull())

    act(() => approvalListener?.())
    await waitFor(() => expect(events('survey shown')).toHaveLength(1))
    expect(result.current.parsed?.survey.id).toBe('0199-nps')
  })

  it('shows a survey that only becomes eligible after an approval', async () => {
    surveys.length = 0
    const { result } = renderHook(() => usePostHogSurvey())
    await waitFor(() => expect(result.current.parsed).toBeNull())

    surveys.push(nps())
    act(() => approvalListener?.())
    await waitFor(() => expect(result.current.parsed?.kind).toBe('nps'))
  })
})
