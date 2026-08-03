import { createContext, useCallback, useContext, useState, useEffect, useRef } from 'react'
import type { ReactNode } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import api from '../api/client'
import { identifyUser, resetAnalytics } from '../utils/analytics'

interface AuthUser {
  email: string
  userId?: number
}

// GET /auth/session — identity plus the non-sensitive person facts PostHog is identified with.
interface SessionDetail {
  user_id: number
  // Non-sequential public identifier (issue #745, 2b) — the id that may appear in a URL or a
  // support ticket. The row id stays server-side.
  public_uid?: string | null
  email: string
  plan?: string | null
  plan_status?: string | null
  timezone?: string | null
  created_at?: string | null
  // What PostHog Surveys target on (issue #653).
  onboarding_completed_at?: string | null
  posts_approved?: number | null
  is_admin?: boolean
  // Mandatory strong-factor enrolment (issue #905). `enrollment_required` is the HARD state — this
  // session is held to the enrolment surface server-side, so the SPA renders the gate instead of
  // the app. `strong_factor_prompt` is the soft one: a deadline exists and nothing is enrolled yet.
  enrollment_required?: boolean
  strong_factor_deadline?: string | null
  strong_factor_prompt?: boolean
}

interface AuthContextValue {
  user: AuthUser | null
  sessionToken: string | null
  isLoading: boolean
  isAdmin: boolean
  /** This session may only enrol a strong factor until it does (issue #905). */
  enrollmentRequired: boolean
  /** A deadline is scheduled and this account still has no factor — show the nudge. */
  strongFactorPrompt: boolean
  strongFactorDeadline: string | null
  /** Re-read /auth/session — the gate calls it once a factor lands, to drop itself. */
  refreshSession: () => Promise<void>
  login: (token: string, email: string) => void
  logout: () => Promise<void>
  openLoginModal: () => void
  closeLoginModal: () => void
  isLoginModalOpen: boolean
}

const AuthContext = createContext<AuthContextValue | null>(null)

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
  const [isLoading, setIsLoading] = useState(true)
  const [isAdmin, setIsAdmin] = useState(false)
  const [enrollmentRequired, setEnrollmentRequired] = useState(false)
  const [strongFactorPrompt, setStrongFactorPrompt] = useState(false)
  const [strongFactorDeadline, setStrongFactorDeadline] = useState<string | null>(null)
  const [isLoginModalOpen, setIsLoginModalOpen] = useState(false)
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
    if (!storedToken) {
      setIsLoading(false)
      return
    }
    loadSession(storedToken)
      .catch(() => {
        localStorage.removeItem(SESSION_KEY)
        localStorage.removeItem('lem_email')
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
    // The token is NOT stored: the login response already set the httpOnly cookie, and what goes
    // into localStorage is the sentinel — a marker that a session exists, worth nothing if stolen.
    localStorage.setItem(SESSION_KEY, COOKIE_SESSION)
    localStorage.setItem('lem_email', email)
    setSessionToken(COOKIE_SESSION)
    setUser({ email })
    setIsLoginModalOpen(false)
    // The verify response carries no user id, so read the session back: it resolves the id every
    // authenticated feature already needs AND the person facts to identify with. A failure here
    // leaves the optimistic (email-only) user in place rather than bouncing a valid login.
    loadSession(COOKIE_SESSION).catch(() => {
      // The cookie did not stick — an http:// origin with Secure cookies, or a browser blocking
      // them. Fall back to holding the token so a valid login is never turned into a lockout; the
      // cookie is the upgrade, not a hard requirement.
      localStorage.setItem(SESSION_KEY, token)
      setSessionToken(token)
      loadSession(token).catch(() => {})
    })
  }

  async function logout() {
    const token = sessionToken
    if (token) {
      await api.post('/auth/logout', { session_token: token }).catch(() => {})
    }
    localStorage.removeItem(SESSION_KEY)
    localStorage.removeItem('lem_email')
    localStorage.removeItem('lem_li_connected')
    localStorage.removeItem('lem_blog_url')
    localStorage.removeItem('lem_sitemap_url')
    // The two-factor nudge is dismissed per BROWSER, so the next person to sign in on this machine
    // would otherwise inherit a dismissal they never made — and never be warned about the deadline.
    localStorage.removeItem('lem_strong_factor_prompt_dismissed')
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
  }

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
        closeLoginModal: () => setIsLoginModalOpen(false),
        isLoginModalOpen,
      }}
    >
      {children}
    </AuthContext.Provider>
  )
}

export function useAuth() {
  const ctx = useContext(AuthContext)
  if (!ctx) throw new Error('useAuth must be used within AuthProvider')
  return ctx
}
