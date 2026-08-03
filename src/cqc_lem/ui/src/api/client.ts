import axios from 'axios'

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
    return Promise.reject(error)
  }
)

export default api
