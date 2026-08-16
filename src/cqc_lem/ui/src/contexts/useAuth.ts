import { createContext, useContext } from 'react'

// The context object and the hook that reads it live apart from the provider component so the
// provider's file exports a component only (Fast Refresh), and so a consumer can import `useAuth`
// without pulling the provider's module graph in.
export interface AuthUser {
  email: string
  userId?: number
}

// GET /auth/session — identity plus the non-sensitive person facts PostHog is identified with.
export interface SessionDetail {
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

export interface AuthContextValue {
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
  /**
   * Why this tab stopped being signed in, when nobody asked it to (issue #1358). Set only when the
   * session was torn down after a 401 was corroborated against `/auth/session` — a deliberate
   * `logout()` leaves it null. The sign-in surface renders it, so a sign-out is never the silent
   * hard redirect that made #1354 read as "I cannot log in".
   */
  sessionEndedReason: string | null
}

export const AuthContext = createContext<AuthContextValue | null>(null)

export function useAuth() {
  const ctx = useContext(AuthContext)
  if (!ctx) throw new Error('useAuth must be used within AuthProvider')
  return ctx
}
