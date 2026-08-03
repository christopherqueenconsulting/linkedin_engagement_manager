import axios from 'axios'
import { NEW_VERSION_MESSAGE, RELOADING_MESSAGE, recoverFromChunkError } from '../utils/chunkReload'

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
  // Since #745 (2b) the session normally rides in an httpOnly cookie the browser attaches itself,
  // and localStorage holds only the 'cookie' sentinel — there is nothing to put in this header.
  // It is still sent when a real token is held (cookie-less fallback, tutorial capture harness).
  const token = localStorage.getItem('lem_session')
  if (token && token !== 'cookie') {
    config.headers['X-Session-Token'] = token
  }
  return config
})

api.interceptors.response.use(
  (r) => r,
  (error) => {
    if (error.response?.status === 401) {
      localStorage.removeItem('lem_session')
      localStorage.removeItem('lem_email')
      window.location.href = '/'
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
