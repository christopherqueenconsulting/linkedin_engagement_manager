// The one place a session teardown is announced (issue #1358).
//
// Before this, the axios response interceptor did the teardown itself: any 401 from any endpoint
// cleared `lem_session` and set `window.location.href = '/'`. On 2026-08-10 (#1354) that turned a
// partial backend failure into a total lockout — `/api/dashboard/stats/` and `/api/activity/` were
// answering 200 the whole time, and the first 401 from a moved route discarded a session the user
// had just been given, then threw away the client state that would have explained it.
//
// So the interceptor no longer owns the teardown; it corroborates and then announces. This event is
// how it reaches the auth layer, which is the only thing that knows every key and every piece of
// state a sign-out has to clear. Same shape as `CHUNK_RELOAD_BLOCKED_EVENT`: a window event so a
// non-React module can tell a React one something happened, without either importing the other.
export const SESSION_ENDED_EVENT = 'lem:session-ended'

// Shown where the user is about to be asked to sign in again. A reason is the point of it — the old
// hard redirect was indistinguishable from "the app randomly logged me out", which is exactly how
// #1354 was reported for most of a working day.
export const SESSION_ENDED_MESSAGE =
  'Your session expired, so you were signed out. Sign in to pick up where you left off.'

/** Tell the app this session is over. Safe to call more than once — the listener is idempotent. */
export function announceSessionEnded(): void {
  window.dispatchEvent(new CustomEvent(SESSION_ENDED_EVENT))
}
