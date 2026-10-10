// The free-trial daily AI limit (issue #2378), announced from the ONE api client so every page
// tells the user the same thing. Same shape as `SESSION_ENDED_EVENT`: a window event lets the
// non-React interceptor reach the React notice without either importing the other.
export const DAILY_AI_LIMIT_EVENT = 'lem:daily-ai-limit'

// The `reason` the server's 429 carries when the free-trial daily AI cap refused the request.
export const DAILY_AI_LIMIT_REASON = 'daily_ai_limit'

// Shown when the response carries no `detail` of its own.
export const DAILY_AI_LIMIT_FALLBACK =
  'Daily AI limit reached for your free trial. AI generation resumes at 00:00 UTC.'

/** Tell the app the daily AI limit refused a request, with the server's own wording. */
export function announceDailyAiLimit(detail?: unknown): void {
  const message = typeof detail === 'string' && detail.trim() ? detail : DAILY_AI_LIMIT_FALLBACK
  window.dispatchEvent(new CustomEvent(DAILY_AI_LIMIT_EVENT, { detail: message }))
}
