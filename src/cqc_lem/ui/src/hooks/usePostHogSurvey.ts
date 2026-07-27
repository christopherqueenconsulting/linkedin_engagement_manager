import { useCallback, useEffect, useRef, useState } from 'react'
import api from '../api/client'
import { useAuth } from '../contexts/AuthContext'
import { FLAGS, useFeatureFlag } from './useFeatureFlags'
import {
  SURVEY_EVENTS,
  activeMatchingSurveys,
  analyticsEnabled,
  capture,
  markSurveySeen,
  onPostApproval,
} from '../utils/analytics'
import type { ActiveSurvey } from '../utils/analytics'

// PostHog Surveys, rendered headless (issue #653, docs/surveys.md).
//
// PostHog owns WHO gets asked and WHEN — targeting on person properties, event triggers and the
// wait-between-surveys throttle, all editable without a deploy. This hook owns the rest: it renders
// the survey in LEM's own modal, emits PostHog's native `survey shown`/`sent`/`dismissed` so the
// response shows up in the Surveys product, AND posts the same answer to the API so it becomes a
// `feedback` row in the auto-work loop. A popover survey would have given us the first half only,
// and a score nobody can act on is not worth asking for.

// The names are the contract with scripts/posthog_surveys.py (which creates the surveys) and
// utilities/surveys.py (which maps a kind onto a feedback source). Matching on name is also the
// guard that stops an unrelated survey someone creates in the PostHog UI from being drawn by a
// component that only knows how to render a rating plus an optional "why".
export const SURVEY_NAMES = {
  nps: 'LEM NPS',
  csat: 'LEM CSAT — post quality',
} as const

export type SurveyKind = keyof typeof SURVEY_NAMES

type RatingQuestion = {
  index: number
  question: string
  scale: number
  min: number
  max: number
  lowerBoundLabel: string
  upperBoundLabel: string
}

type OpenQuestion = { index: number; question: string }

export interface ParsedSurvey {
  kind: SurveyKind
  survey: ActiveSurvey
  rating: RatingQuestion
  open: OpenQuestion | null
}

function kindOf(name: string | undefined): SurveyKind | null {
  const match = (Object.keys(SURVEY_NAMES) as SurveyKind[]).find((k) => SURVEY_NAMES[k] === name)
  return match ?? null
}

// PostHog's 10-point rating IS the 0-10 NPS band; every other scale starts at 1. That single rule
// is why the same component can draw both surveys.
function boundsFor(scale: number): { min: number; max: number } {
  return scale === 10 ? { min: 0, max: 10 } : { min: 1, max: scale }
}

/** The first LEM survey in `surveys` we know how to draw, or null. Pure — the modal's whole
 * decision, testable without posthog-js. */
export function parseSurvey(surveys: ActiveSurvey[]): ParsedSurvey | null {
  for (const survey of surveys ?? []) {
    const kind = kindOf(survey?.name)
    if (!kind) continue
    const questions = survey.questions ?? []
    const ratingIndex = questions.findIndex((q) => q.type === 'rating')
    if (ratingIndex < 0) continue
    const rating = questions[ratingIndex] as { question: string; scale?: number }
    const scale = Number(rating.scale) || 10
    const openIndex = questions.findIndex((q) => q.type === 'open')
    return {
      kind,
      survey,
      rating: {
        index: ratingIndex,
        question: rating.question,
        scale,
        ...boundsFor(scale),
        lowerBoundLabel:
          (questions[ratingIndex] as { lowerBoundLabel?: string }).lowerBoundLabel || '',
        upperBoundLabel:
          (questions[ratingIndex] as { upperBoundLabel?: string }).upperBoundLabel || '',
      },
      open:
        openIndex >= 0
          ? { index: openIndex, question: questions[openIndex].question }
          : null,
    }
  }
  return null
}

/** The identity properties every survey event carries, so `survey shown` and `survey sent` line up
 * on the same survey in PostHog. */
export function surveyEventProps(survey: ActiveSurvey): Record<string, unknown> {
  return {
    $survey_id: survey.id,
    $survey_name: survey.name,
    $survey_iteration: survey.current_iteration ?? null,
    $survey_iteration_start_date: survey.current_iteration_start_date ?? null,
  }
}

/** `survey sent` properties in PostHog's own shape: `$survey_response` for the first question,
 * `$survey_response_<n>` for the rest, the by-question-id keys the newer schema reads, and
 * `$survey_questions` carrying the text alongside each answer. Emitting what the SDK emits is what
 * makes a headless response indistinguishable from a popover one in the Surveys product. */
export function surveyResponseProps(
  survey: ActiveSurvey,
  answers: Array<string | number | null>
): Record<string, unknown> {
  const props: Record<string, unknown> = { ...surveyEventProps(survey), $survey_completed: true }
  const questions = (survey.questions ?? []).map((question, index) => {
    const response = answers[index] ?? null
    const filled = response !== null && response !== ''
    if (filled) {
      props[index === 0 ? '$survey_response' : `$survey_response_${index}`] = response
      if (question.id) props[`$survey_response_${question.id}`] = response
    }
    return { id: question.id, question: question.question, response }
  })
  props.$survey_questions = questions
  return props
}

export interface PostHogSurveyState {
  parsed: ParsedSurvey | null
  submit: (score: number, comment: string) => Promise<void>
  dismiss: () => void
}

/** The PostHog survey to show right now, plus the two ways it ends. */
export function usePostHogSurvey(): PostHogSurveyState {
  const { sessionToken } = useAuth()
  const enabled = useFeatureFlag(FLAGS.posthogSurveys, false)
  const [parsed, setParsed] = useState<ParsedSurvey | null>(null)
  const [closed, setClosed] = useState(false)
  // An approval is the ONE moment CSAT eligibility can newly become true, so it re-runs the check
  // with a forced reload instead of the SPA polling PostHog for a state that changes twice a month.
  const [approvals, setApprovals] = useState(0)
  // Ask-once bookkeeping for THIS page load. markSurveySeen() covers reloads; this covers the
  // re-check an approval triggers while the same survey is already on screen.
  const shown = useRef<string | null>(null)
  // The survey we have already emitted `survey sent` for. It makes the response event idempotent
  // across a retry after a failed POST, and it is what stops the "Done" button on the thank-you
  // screen recording a DISMISSAL of a survey the user actually completed.
  const sent = useRef<string | null>(null)

  useEffect(() => onPostApproval(() => setApprovals((n) => n + 1)), [])

  useEffect(() => {
    // Nothing to clear on the way out: with the flag off (or analytics off, or logged out) nothing
    // was ever set, and a logout unmounts the modal Layout only renders for a signed-in user.
    if (!enabled || !sessionToken || !analyticsEnabled()) return
    let live = true
    activeMatchingSurveys((surveys) => {
      if (!live) return
      const next = parseSurvey(surveys)
      if (!next) return
      if (shown.current === next.survey.id) return
      shown.current = next.survey.id
      setClosed(false)
      setParsed(next)
      capture(SURVEY_EVENTS.shown, surveyEventProps(next.survey))
      // Record the ask with posthog-js itself, or its seen/wait-period checks — the throttle the
      // whole design leans on — would never know this survey was displayed.
      markSurveySeen(next.survey.id, next.survey.current_iteration)
    }, approvals > 0)
    return () => {
      live = false
    }
  }, [enabled, sessionToken, approvals])

  const dismiss = useCallback(() => {
    setClosed(true)
    if (!parsed || sent.current === parsed.survey.id) return
    capture(SURVEY_EVENTS.dismissed, surveyEventProps(parsed.survey))
  }, [parsed])

  const submit = useCallback(
    async (score: number, comment: string) => {
      if (!parsed) return
      const answers: Array<string | number | null> = []
      answers[parsed.rating.index] = score
      if (parsed.open) answers[parsed.open.index] = comment.trim() || null
      // PostHog first: the analytics record of the response must not depend on LEM's API being up.
      // Once per survey, so a retry after a failed POST doesn't count the answer twice.
      if (sent.current !== parsed.survey.id) {
        sent.current = parsed.survey.id
        capture(SURVEY_EVENTS.sent, surveyResponseProps(parsed.survey, answers))
      }
      await api.post('/survey/posthog', {
        session_token: sessionToken,
        kind: parsed.kind,
        score,
        comment: comment.trim() || null,
        survey_id: parsed.survey.id,
        survey_name: parsed.survey.name,
      })
    },
    [parsed, sessionToken]
  )

  return { parsed: closed ? null : parsed, submit, dismiss }
}
