// BCP-47 tags kept in lockstep with users.content_language VARCHAR(16). Empty = inherit the
// Login Location locale. Drives the language of premium (Veo) video audio — issue #548.
export const CONTENT_LANGUAGE_OPTIONS = [
  { label: 'English (US)', value: 'en-US' },
  { label: 'English (UK)', value: 'en-GB' },
  { label: 'Spanish', value: 'es-ES' },
  { label: 'Spanish (Latin America)', value: 'es-419' },
  { label: 'French', value: 'fr-FR' },
  { label: 'German', value: 'de-DE' },
  { label: 'Portuguese (Brazil)', value: 'pt-BR' },
  { label: 'Italian', value: 'it-IT' },
  { label: 'Dutch', value: 'nl-NL' },
  { label: 'Japanese', value: 'ja-JP' },
  { label: 'Korean', value: 'ko-KR' },
  { label: 'Chinese (Simplified)', value: 'zh-CN' },
  { label: 'Hindi', value: 'hi-IN' },
  { label: 'Arabic', value: 'ar-SA' },
]

// Publishing cadence (issue #621). Each step adds one weekday to the fixed day-type calendar in the
// order the planner fills it (see POST_DAY_PRIORITY). 2-4/week is the 2026 sweet spot; 5+ is offered
// but flagged, because daily posting costs ~26% of each post's average reach. The days are no longer
// spelled into the labels — which ones a step lands on now depends on `posting_days` (issue #581),
// so the resolved days are rendered live underneath the control instead.
export const CADENCE_OPTIONS = [
  { label: '2 a week', value: 2 },
  { label: '3 a week — recommended', value: 3 },
  { label: '4 a week', value: 4 },
  { label: '5 a week', value: 5 },
  { label: '6 a week', value: 6 },
  { label: '7 a week (daily)', value: 7 },
]

// Mon=0 … Sun=6, in the SPA's own display order.
export const WEEKDAY_OPTIONS = [
  { label: 'Mon', value: 0 },
  { label: 'Tue', value: 1 },
  { label: 'Wed', value: 2 },
  { label: 'Thu', value: 3 },
  { label: 'Fri', value: 4 },
  { label: 'Sat', value: 5 },
  { label: 'Sun', value: 6 },
]

export const DEFAULT_POSTING_DAYS = [0, 1, 2, 3, 4]

// The order the day-type calendar fills its slots, mirroring POST_DAY_TYPES' `priority` in
// utilities/ai/content_framework.py: Tue build receipt, Thu spiky POV, Wed story, Mon guide,
// Fri observation, Sun reflection, Sat conversation.
export const POST_DAY_PRIORITY = [1, 3, 2, 0, 4, 6, 5]

/** The weekdays a plan actually publishes on — `weekly_post_slots()` in the planner, in TS.
 *  Cadence says how many; `allowedDays` says which are eligible, and caps the count. */
export function weeklyPostSlots(postsPerWeek: number, allowedDays?: number[] | null): number[] {
  const allowed = allowedDays?.length ? allowedDays : DEFAULT_POSTING_DAYS
  const candidates = POST_DAY_PRIORITY.filter((d) => allowed.includes(d))
  const pool = candidates.length ? candidates : POST_DAY_PRIORITY
  const count = Math.max(1, Math.min(pool.length, Number(postsPerWeek) || 0))
  return pool.slice(0, count).sort((a, b) => a - b)
}

export const weekdayLabels = (days: number[]): string =>
  days.map((d) => WEEKDAY_OPTIONS.find((o) => o.value === d)?.label ?? String(d)).join(', ')

export const INACTIVATE_OPTIONS = [
  { label: '30 days', value: 30 },
  { label: '60 days', value: 60 },
  { label: '90 days (default)', value: 90 },
  { label: '120 days', value: 120 },
  { label: '365 days', value: 365 },
  { label: 'Never', value: null },
]
