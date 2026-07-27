// Progress of a weekly content-generation run (issue #545) and the copy for a run that finished
// with nothing to do (issue #719). Kept out of ContentStudio.tsx so the wording is unit-testable:
// "0 posts are ready to review" with no reason is what made a working button read as broken.

import { formatInTimezone } from './datetime'

export type GenerationState = 'queued' | 'in_progress' | 'done' | 'failed'
export type GenerationEmptyReason = 'buffer_full' | 'no_planned_slots' | 'already_running'

export interface GenerationStatus {
  state: GenerationState
  total: number
  completed: number
  failed: number
  post_ids: number[]
  ready_post_ids: number[]
  failed_post_ids: number[]
  started_at: string | null
  finished_at: string | null
  updated_at?: string | null
  // Absent on runs that generated posts — only an empty run carries a reason.
  reason?: GenerationEmptyReason | null
  reason_detail?: {
    next_planned_at?: string | null
    buffer_days?: number | null
    ready_count?: number | null
    buffer_max?: number | null
  } | null
}

export const isGenerationRunning = (s?: GenerationStatus | null) =>
  !!s && (s.state === 'queued' || s.state === 'in_progress')

// A run that generated nothing is an ANSWER, not a failure — so say why and what happens next.
// Returns null whenever the default "N posts are ready to review" line is the right one.
export function emptyRunExplanation(
  s: GenerationStatus,
  tz: string,
): { headline: string; detail: string } | null {
  if (s.state !== 'done' || s.total > 0 || !s.reason) return null
  const d = s.reason_detail ?? {}
  // Date only: the hour a slot happens to sit at is noise in an explanation.
  const nextPlanned = d.next_planned_at
    ? formatInTimezone(d.next_planned_at, tz, {
        month: 'short',
        day: 'numeric',
        hour: undefined,
        minute: undefined,
      })
    : null
  const bufferDays = d.buffer_days ?? 5
  const days = `${bufferDays} ${bufferDays === 1 ? 'day' : 'days'}`

  if (s.reason === 'already_running') {
    return {
      headline: 'A generation run is already in progress',
      detail: "We didn't start a second one — give the current run a few minutes to finish.",
    }
  }
  if (s.reason === 'buffer_full') {
    const ready = d.ready_count ?? 0
    return {
      headline: "Nothing to generate right now — you're already stocked up",
      detail:
        `${ready} ${ready === 1 ? 'post is' : 'posts are'} already generated and waiting for you. ` +
        `We generate more automatically as those get published.` +
        (nextPlanned ? ` Your next planned post is ${nextPlanned}.` : ''),
    }
  }
  return {
    headline: 'Nothing to generate right now — no slots are due yet',
    detail: nextPlanned
      ? `Your next planned post is ${nextPlanned}. Generation starts automatically about ` +
        `${days} before each planned slot.`
      : 'Your plan has no upcoming posts left to generate. The next month is planned ' +
        'automatically — or add one now with "+ Schedule a post".',
  }
}
