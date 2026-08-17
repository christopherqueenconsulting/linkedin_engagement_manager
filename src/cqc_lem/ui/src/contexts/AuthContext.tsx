import { useCallback, useState, useEffect, useRef } from 'react'
import type { ReactNode } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import api from '../api/client'
import { AuthContext } from './useAuth'
import type { AuthUser, SessionDetail } from './useAuth'
import { identifyUser, resetAnalytics } from '../utils/analytics'
import { SESSION_ENDED_EVENT, SESSION_ENDED_MESSAGE } from '../utils/sessionEnd'
import { clearFallbackSessionCookie, writeFallbackSessionCookie } from '../utils/sessionCookie'

const SESSION_KEY = 'lem_session'

/**
 * Since issue #745 (2b) the session token lives in an httpOnly cookie the browser attaches to every
 * same-origin request, so no script on this page — ours or an injected one — can read it. What
 * `sessionToken` carries is this non-secret sentinel: the ~150 call sites that pass
 * `session_token` keep their shape, and the API resolves the request from the cookie instead.
 * A real token is only ever held as the fallback below.
 */
export const COOKIE_SESSION = 'cookie'

export function AuthProvider({ children }: { children: ReactNode }) {
  const queryClient = useQueryClient()
  const [user, setUser] = useState<AuthUser | null>(null)
  const [sessionToken, setSessionToken] = useState<string | null>(null)
  // Loading means "a stored session is being re-read". With nothing stored there is nothing to
  // wait for, so a logged-out visitor never renders a loading shell it would immediately drop.
  const [isLoading, setIsLoading] = useState(() => !!localStorage.getItem(SESSION_KEY))
  const [isAdmin, setIsAdmin] = useState(false)
  const [enrollmentRequired, setEnrollmentRequired] = useState(false)
  const [strongFactorPrompt, setStrongFactorPrompt] = useState(false)
  const [strongFactorDeadline, setStrongFactorDeadline] = useState<string | null>(null)
  const [isLoginModalOpen, setIsLoginModalOpen] = useState(false)
  // Why the app stopped being signed in, when it stopped on its own (issue #1358). Null for a
  // sign-out the user asked for — they know why that happened.
  const [sessionEndedReason, setSessionEndedReason] = useState<string | null>(null)
  // Identify once per user per page load — re-identifying on every session check would burn a
  // $identify on each mount for no new information.
  const identifiedUserId = useRef<number | null>(null)

  function applySession(detail: SessionDetail, token: string) {
    setSessionToken(token)
    setUser({ email: detail.email, userId: detail.user_id })
    setIsAdmin(!!detail.is_admin)
    setEnrollmentRequired(!!detail.enrollment_required)
    setStrongFactorPrompt(!!detail.strong_factor_prompt)
    setStrongFactorDeadline(detail.strong_factor_deadline ?? null)
    if (detail.user_id && identifiedUserId.current !== detail.user_id) {
      identifiedUserId.current = detail.user_id
      identifyUser({
        userId: detail.user_id,
        email: detail.email,
        plan: detail.plan,
        planStatus: detail.plan_status,
        timezone: detail.timezone,
        createdAt: detail.created_at,
        onboardingCompletedAt: detail.onboarding_completed_at,
        postsApproved: detail.posts_approved,
      })
    }
  }

  function loadSession(token: string): Promise<void> {
    return api
      .get(`/auth/session?session_token=${encodeURIComponent(token)}`)
      .then((r) => applySession(r.data.detail as SessionDetail, token))
  }

  useEffect(() => {
    const storedToken = localStorage.getItem(SESSION_KEY)
    if (!storedToken) return
    // A stored value that is NOT the sentinel is a cookie-less fallback session (issue #1611), and
    // this is the first thing that runs in it: re-write the cookie the `/api` edge gate reads before
    // any panel asks for anything. Needed on every boot, not only after `login()` — the browser that
    // refused the server's cookie may equally have dropped ours, and a session stored by a bundle
    // that predates this one has no cookie at all.
    if (storedToken !== COOKIE_SESSION) writeFallbackSessionCookie(storedToken)
    loadSession(storedToken)
      .catch(() => {
        localStorage.removeItem(SESSION_KEY)
        localStorage.removeItem('lem_email')
        // Abandoning the stored session has to abandon its cookie with it. This branch is reached on
        // ANY failure, including a 5xx or a dropped connection, so the token may still be LIVE — and
        // unlike the server's own cookie, a script-written one is not replaced by the next login's
        // `Set-Cookie` in the very browser that refuses it. Leaving it would let the next person to
        // sign in on this machine have `loadSession(COOKIE_SESSION)` answered by the PREVIOUS
        // session's cookie, i.e. signed in as somebody else.
        clearFallbackSessionCookie()
      })
      .finally(() => setIsLoading(false))
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  // Memoised on the token, and that is load-bearing rather than tidiness: the enrolment gate waits
  // on this in an effect, so a fresh closure every render would re-fire the effect on the state
  // update this very call produces — one /auth/session request per render, forever.
  const refreshSession = useCallback(
    () => (sessionToken ? loadSession(sessionToken) : Promise.resolve()),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [sessionToken],
  )

  function login(token: string, email: string) {
    // Any script-written cookie still here belongs to a session this login replaces, and the probe
    // below asks the server to resolve THE COOKIE. The server's own `Set-Cookie` has already landed
    // by now and shadows this write (httpOnly), so on a normal login it is a no-op — but in the
    // cookie-refusing browser this whole fallback exists for, nothing else would ever overwrite the
    // stale one, and the probe would come back as its owner rather than as `token`'s.
    clearFallbackSessionCookie()
    // The token is NOT stored: the login response already set the httpOnly cookie, and what goes
    // into localStorage is the sentinel — a marker that a session exists, worth nothing if stolen.
    localStorage.setItem(SESSION_KEY, COOKIE_SESSION)
    localStorage.setItem('lem_email', email)
    setSessionToken(COOKIE_SESSION)
    setUser({ email })
    setIsLoginModalOpen(false)
    setSessionEndedReason(null)
    // The verify response carries no user id, so read the session back: it resolves the id every
    // authenticated feature already needs AND the person facts to identify with. A failure here
    // leaves the optimistic (email-only) user in place rather than bouncing a valid login.
    loadSession(COOKIE_SESSION).catch(() => {
      // The cookie did not stick — an http:// origin with Secure cookies, or a browser blocking
      // them. Fall back to holding the token so a valid login is never turned into a lockout; the
      // cookie is the upgrade, not a hard requirement.
      //
      // Holding it in localStorage alone was NOT enough (issue #1611): the route resolves the token
      // from the `session_token` field, but the `/api` edge gate runs before routing and reads only
      // a bearer or the `lem_session` cookie, so with API_ACCESS_TOKENS set every panel 401'd. So
      // write the same token into a cookie this browser will actually keep — see `sessionCookie.ts`
      // for why that costs no exposure the localStorage copy did not already cost.
      writeFallbackSessionCookie(token)
      localStorage.setItem(SESSION_KEY, token)
      setSessionToken(token)
      loadSession(token).catch(() => {})
    })
  }

  // Everything a sign-out clears in THIS browser, with no server call in it. Shared by the
  // deliberate `logout()` below and by the session-ended listener (issue #1358), which has nothing
  // left to tell the server: the session it would be revoking is the one that just proved gone.
  const clearLocalSession = useCallback(() => {
    localStorage.removeItem(SESSION_KEY)
    localStorage.removeItem('lem_email')
    localStorage.removeItem('lem_li_connected')
    localStorage.removeItem('lem_blog_url')
    localStorage.removeItem('lem_sitemap_url')
    // The two-factor nudge is dismissed per BROWSER, so the next person to sign in on this machine
    // would otherwise inherit a dismissal they never made — and never be warned about the deadline.
    localStorage.removeItem('lem_strong_factor_prompt_dismissed')
    // The fallback's own credential (issue #1611). It is JS-written, so it is JS-cleared — leaving
    // it behind would keep handing the edge gate a token this browser has finished with. A no-op on
    // a normal session: a browser discards a `document.cookie` write aimed at an httpOnly cookie.
    clearFallbackSessionCookie()
    setSessionToken(null)
    setUser(null)
    setIsAdmin(false)
    setEnrollmentRequired(false)
    setStrongFactorPrompt(false)
    // Every cached response in this tab belongs to the person who just signed out. Since #745 the
    // session token is the same non-secret sentinel for everybody, so a cache key carrying it
    // carries NO identity — sign out, sign in as somebody else in the same tab, and React Query
    // serves the previous account's dashboard from cache until the first refetch lands. Clearing
    // here is the structural half; the per-query keys below carry the user id as well.
    queryClient.clear()
    // Break the browser↔person link, or the next person to sign in on this machine inherits it.
    identifiedUserId.current = null
    resetAnalytics()
  }, [queryClient])

  async function logout() {
    const token = sessionToken
    if (token) {
      await api.post('/auth/logout', { session_token: token }).catch(() => {})
    }
    clearLocalSession()
    setSessionEndedReason(null)
  }

  // The session went away on its own — the API client corroborated a 401 against /auth/session and
  // got a 401 back (issue #1358). Tearing down HERE, rather than in the interceptor, is what makes
  // it a state change instead of a page navigation: React Router moves the tab to the logged-out
  // tree, the client state and any error boundary survive, and the reason is something we can show
  // rather than something the user has to infer from having been dumped on the front page.
  useEffect(() => {
    const onSessionEnded = () => {
      clearLocalSession()
      setSessionEndedReason(SESSION_ENDED_MESSAGE)
      setIsLoginModalOpen(true)
      // A boot that is still in flight would otherwise leave the app on its loading shell forever.
      setIsLoading(false)
    }
    window.addEventListener(SESSION_ENDED_EVENT, onSessionEnded)
    return () => window.removeEventListener(SESSION_ENDED_EVENT, onSessionEnded)
  }, [clearLocalSession])

  return (
    <AuthContext.Provider
      value={{
        user,
        sessionToken,
        isLoading,
        isAdmin,
        enrollmentRequired,
        strongFactorPrompt,
        strongFactorDeadline,
        // The gate calls this after a factor lands: the server has already promoted the session
        // out of the enrolment scope, so re-reading it is what makes the gate disappear.
        refreshSession,
        login,
        logout,
        openLoginModal: () => setIsLoginModalOpen(true),
        // Dismissing the modal dismisses the notice with it — re-opening it later to sign in is a
        // fresh intent, not a second report of a sign-out that already happened.
        closeLoginModal: () => { setIsLoginModalOpen(false); setSessionEndedReason(null) },
        isLoginModalOpen,
        sessionEndedReason,
      }}
    >
      {children}
    </AuthContext.Provider>
  )
}
