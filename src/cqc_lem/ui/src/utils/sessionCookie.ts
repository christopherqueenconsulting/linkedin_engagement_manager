// The credential the COOKIE-LESS login fallback carries (issue #1611).
//
// `AuthContext.login()` falls back to holding the real session token when the browser refused the
// httpOnly one the login response set — a plain-http origin dropping a `Secure` cookie, a browser
// blocking third-party-ish cookies. In that state the token authenticates fine at the ROUTE, which
// resolves an explicit token from the `session_token` FIELD every call site already sends. It does
// not get that far: `api_token_middleware` is an edge filter that runs BEFORE routing, has no
// database and must not consume a POST body, so all it can read is a bearer or the `lem_session`
// cookie — and the fallback has neither. With `API_ACCESS_TOKENS` set (it is, in production) every
// non-`/api/auth/` request from such a session is 401'd before the resolver ever runs, which reads
// as a broken app rather than a broken session. Writing the token into a first-party cookie the
// browser WILL keep hands that session the one credential the edge check and the resolver both
// already read, and adds no server surface.
//
// Exposure is unchanged: the fallback already holds this exact token in `localStorage`, which is
// equally readable by any script on the page. The cookie cannot be httpOnly (`document.cookie` may
// not set that). CSRF is unaffected: a cookie-authenticated write still has to carry
// `X-LEM-Client` (#957), which a cross-site form cannot set, and `SameSite=Lax` keeps the cookie off
// cross-site writes exactly as the server's own does.
//
// `Secure` is the ONE attribute that tracks the page instead of being fixed. On a plain-http origin
// it has to be absent — a refused `Secure` cookie is the whole failure this exists for, and a
// browser drops a `Secure` cookie written from http anyway, so setting it there would refuse the fix
// to the case it was written for. On an https origin it has to be PRESENT: `localStorage` is scoped
// by scheme, a cookie is not, so a non-`Secure` cookie hands the live token to the first `http://`
// request anyone makes to this host, in cleartext — an exposure the `localStorage` copy never had.
// The https fallback is reachable (a proxy stripping `Set-Cookie`, a browser refusing the server's
// cookie), so this is not a theoretical branch.

// Must match the server's `SESSION_COOKIE_NAME` (`utilities/env_constants.py`, default
// `lem_session`) — both `_has_session_credential` and `session_cookie_middleware` read the cookie
// under that name, so a deployment that overrides it also has to override this.
export const SESSION_COOKIE_NAME = 'lem_session'

// Mirrors the server's `SESSION_ABSOLUTE_MAX_DAYS` default (30). Outliving the session is harmless —
// the resolver refuses a dead token and #1358's corroboration ends the session — but expiring FIRST
// would drop the tab straight back into the 401 this fixes.
const MAX_AGE_SECONDS = 30 * 24 * 3600

/**
 * `Path=/; SameSite=Lax`, plus `Secure` exactly when the page itself is secure (see the header).
 *
 * The scheme is read off `document.URL` rather than `location.protocol`: they answer the same
 * question, and jsdom's `location` is unforgeable, so reading it there would make the https branch —
 * the one that matters for exposure — the one branch no test could reach.
 */
function attributes(): string {
  const base = 'Path=/; SameSite=Lax'
  return document.URL.startsWith('https:') ? `${base}; Secure` : base
}

/**
 * Carry the fallback session's real token in the cookie the `/api` edge gate reads.
 *
 * No-op without a token or a `document` (the server-render/test case). The token is
 * `secrets.token_hex(32)` server-side — hex only — so it needs no escaping to survive cookie
 * parsing, which does not percent-decode.
 */
export function writeFallbackSessionCookie(token: string): void {
  if (typeof document === 'undefined' || !token) return
  document.cookie = `${SESSION_COOKIE_NAME}=${token}; Max-Age=${MAX_AGE_SECONDS}; ${attributes()}`
}

/**
 * Drop it whenever this browser stops holding the session — a sign-out, a session that ended on its
 * own, a stored session abandoned at boot, or a fresh `login()` replacing whatever came before.
 *
 * Safe on a normal cookie session too: a browser discards a `document.cookie` write aimed at an
 * httpOnly cookie, so this can only ever remove the one this module wrote.
 */
export function clearFallbackSessionCookie(): void {
  if (typeof document === 'undefined') return
  document.cookie = `${SESSION_COOKIE_NAME}=; Max-Age=0; ${attributes()}`
}
