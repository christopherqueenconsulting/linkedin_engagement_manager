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
  email: string
  plan?: string | null
  plan_status?: string | null
  timezone?: string | null
  created_at?: string | null
}

interface AuthContextValue {
  user: AuthUser | null
  sessionToken: string | null
  isLoading: boolean
  login: (token: string, email: string) => void
  logout: () => Promise<void>
  openLoginModal: () => void
  closeLoginModal: () => void
  isLoginModalOpen: boolean
}

const AuthContext = createContext<AuthContextValue | null>(null)

const SESSION_KEY = 'lem_session'

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<AuthUser | null>(null)
  const [sessionToken, setSessionToken] = useState<string | null>(null)
  const [isLoading, setIsLoading] = useState(true)
  const [isLoginModalOpen, setIsLoginModalOpen] = useState(false)
  // Identify once per user per page load — re-identifying on every session check would burn a
  // $identify on each mount for no new information.
  const identifiedUserId = useRef<number | null>(null)

  function applySession(detail: SessionDetail, token: string) {
    setSessionToken(token)
    setUser({ email: detail.email, userId: detail.user_id })
    if (detail.user_id && identifiedUserId.current !== detail.user_id) {
      identifiedUserId.current = detail.user_id
      identifyUser({
        userId: detail.user_id,
        email: detail.email,
        plan: detail.plan,
        planStatus: detail.plan_status,
        timezone: detail.timezone,
        createdAt: detail.created_at,
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
    localStorage.setItem(SESSION_KEY, token)
    localStorage.setItem('lem_email', email)
    setSessionToken(token)
    setUser({ email })
    setIsLoginModalOpen(false)
    // The verify response carries no user id, so read the session back: it resolves the id every
    // authenticated feature already needs AND the person facts to identify with. A failure here
    // leaves the optimistic (email-only) user in place rather than bouncing a valid login.
    loadSession(token).catch(() => {})
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
