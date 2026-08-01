import { createContext, useContext, useState, useEffect, useRef } from 'react'
import type { ReactNode } from 'react'
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
}

interface AuthContextValue {
  user: AuthUser | null
  sessionToken: string | null
  isLoading: boolean
  isAdmin: boolean
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
  const [user, setUser] = useState<AuthUser | null>(null)
  const [sessionToken, setSessionToken] = useState<string | null>(null)
  const [isLoading, setIsLoading] = useState(true)
  const [isAdmin, setIsAdmin] = useState(false)
  const [isLoginModalOpen, setIsLoginModalOpen] = useState(false)
  // Identify once per user per page load — re-identifying on every session check would burn a
  // $identify on each mount for no new information.
  const identifiedUserId = useRef<number | null>(null)

  function applySession(detail: SessionDetail, token: string) {
    setSessionToken(token)
    setUser({ email: detail.email, userId: detail.user_id })
    setIsAdmin(!!detail.is_admin)
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
    setSessionToken(null)
    setUser(null)
    setIsAdmin(false)
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
