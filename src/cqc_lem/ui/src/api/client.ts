import axios from 'axios'
import type { InternalAxiosRequestConfig } from 'axios'
import { NEW_VERSION_MESSAGE, RELOADING_MESSAGE, recoverFromChunkError } from '../utils/chunkReload'
import { announceSessionEnded } from '../utils/sessionEnd'

// `baseURL` must stay RELATIVE. It is what makes every request same-origin whatever the host (dev
// server, docker-compose, the prod nginx edge), and a custom header on a same-origin request is
// never preflighted — an absolute baseURL would put `X-LEM-Client` (below) behind a CORS preflight
// the server answers nothing to, i.e. it would break every request rather than just the writes.
const api = axios.create({
  baseURL: '/api',
  headers: { 'Content-Type': 'application/json' },
})

// No LEM-issued shared secret is built into this bundle (issue #950). The SPA used to ship
// VITE_API_TOKEN — one of the server's API_ACCESS_TOKENS, inlined at build time — which made it a
// secret held by everyone who had ever loaded the page, unrotatable without a rebuild + redeploy,
// and worth nothing as a gate. The SPA authenticates on its httpOnly session cookie, which is what
// every /api handler resolves the caller from anyway (#914). The bearer survives server-side as a
// NON-BROWSER credential only (scripts, Postman, admin tooling).
// `.github/workflows/ui-build.yml` builds with canary values and greps dist/ to keep that true.

// NOT a secret, and it must never be turned into one (issue #957). A cross-origin HTML form cannot
// set a request header at all — whatever its value would have been — and setting one from fetch()
// needs a preflight the server answers nothing to. That is the whole mechanism: the server checks
// that the header is PRESENT on a cookie-authenticated write, never what it says. Sent on every
// request from this one client so no call site has to remember it.
const CLIENT_HEADER = 'X-LEM-Client'

api.interceptors.request.use((config) => {
  config.headers[CLIENT_HEADER] = 'spa'
  // A multipart upload must NOT inherit the JSON default above (issue #1030). axios reads the
  // Content-Type in `transformRequest`, and when it says JSON it serialises a FormData body with
  // `JSON.stringify(formDataToJSON(data))` — the File collapses to `{}`, so the bytes never leave
  // the browser and the endpoint answers 422. Clearing it here lets the adapter hand the body to
  // the browser, which is the only thing that can write the multipart boundary. It belongs in this
  // one client for the same reason the header above does: a call site that has to remember it is a
  // call site that will forget it — `/user/newsletter-draft/cover` already had.
  // `multipart/form-data` here carries no boundary, and it is not meant to: the adapter replaces it
  // with the browser's own value (boundary included) for a FormData body. Saying it anyway is what
  // `Avatars.tsx` already does per call site, and it keeps the intent readable.
  if (typeof FormData !== 'undefined' && config.data instanceof FormData) {
    config.headers['Content-Type'] = 'multipart/form-data'
  }
  // No `X-Session-Token` header is sent (#1357). The server never resolved a user from it — an
  // explicit token authenticates from the `session_token` FIELD, which every call site already
  // sends — so it read only as a presence check at the edge gate, and that read is gone. Sending a
  // real token in a header nothing consumes is exposure without a payer.
  return config
})

// The ONE route whose 401 is an answer about the SESSION rather than about an endpoint (#1358).
// Every other 401 says only that this request was not served — a route that lost its cookie
// resolution (#1154/#1354), a scope narrowing, a handler asking the wrong question. Promoting one
// of those to a global sign-out is what turned a partial outage into a lockout, so a 401 elsewhere
// is now corroborated here before anything is torn down.
const SESSION_ROUTE = '/auth/session'

// A 401 from the session route is the auth layer's own business, and is deliberately NOT handled
// here: `AuthProvider` boots on it (clear the stored sentinel) and `login()` answers it by falling
// back to holding the token, which a teardown from underneath would break.
function isSessionRoute(config: InternalAxiosRequestConfig | undefined): boolean {
  return (config?.url ?? '').split('?')[0].endsWith(SESSION_ROUTE)
}

/** The corroborating request while it is in flight — one per burst, not one per 401. */
let sessionProbe: Promise<void> | null = null

/**
 * Ask `/auth/session` whether the session is actually gone, and end it only if the answer is yes.
 *
 * One extra request, on a path that is already failing, and deduped across a burst — a dashboard
 * mount fires a dozen requests at once, and twelve simultaneous 401s are still one question.
 * Anything other than a 401 back (200, a 5xx, a network failure) leaves the session alone: the
 * absence of proof that it died is not proof that it died.
 *
 * The stored value is sent as `session_token`, exactly as `AuthContext.loadSession` sends it, and
 * that is load-bearing rather than symmetry: that FIELD is the only place the server resolves an
 * explicit token from — there is no header form of it (#1357). In the cookie-less fallback
 * (`login()` holds a real token because the browser refused the cookie) a probe without the field
 * carries no credential the resolver reads, so it would 401 about a perfectly live session — the
 * corroboration becoming the single point of amplification it was added to remove. Normally the
 * value is the non-secret `cookie` sentinel, which the server ignores in favour of the cookie.
 */
function confirmSessionEnded(storedToken: string): Promise<void> {
  if (!sessionProbe) {
    sessionProbe = api
      .get(SESSION_ROUTE, { params: { session_token: storedToken } })
      .then(() => undefined)
      .catch((error) => {
        if (error?.response?.status === 401) {
          localStorage.removeItem('lem_session')
          localStorage.removeItem('lem_email')
          announceSessionEnded()
        }
      })
      .finally(() => { sessionProbe = null })
  }
  return sessionProbe
}

api.interceptors.response.use(
  (r) => r,
  (error) => {
    // No stored session means there is nothing to tear down and nothing to corroborate — a signed
    // out visitor's 401 is just a 401. It is also what stops a burst of 401s AFTER a teardown from
    // re-probing: the sentinel is gone by then.
    if (error.response?.status === 401 && !isSessionRoute(error.config)) {
      const storedToken = localStorage.getItem('lem_session')
      if (storedToken) {
        void confirmSessionEnded(storedToken)
      }
    }
    // The server refused because this bundle sent no X-LEM-Client (issue #957) — it predates the
    // release that added it, i.e. a tab left open across that deploy. That is the stale-bundle case
    // the chunk-reload guard already owns, so reuse it rather than growing a second reload path:
    // ONE reload lands on a bundle that sends the header, and the guard turns a second failure
    // inside the cooldown into a message instead of a loop. The 403 is deliberately not a 401 —
    // signing the user out would be the wrong fix for "your app is stale".
    if (error.response?.status === 403 &&
        error.response?.data?.detail?.code === 'client_header_required') {
      const outcome = recoverFromChunkError(error, { force: true })
      return Promise.reject(new Error(
        outcome === 'reloaded' ? RELOADING_MESSAGE : NEW_VERSION_MESSAGE, { cause: error }))
    }
    return Promise.reject(error)
  }
)

export default api
