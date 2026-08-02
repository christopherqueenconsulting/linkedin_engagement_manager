import { useEffect, useRef } from 'react'
import { useQuery } from '@tanstack/react-query'
import api from '../api/client'
import { useAuth } from '../contexts/AuthContext'
import AuthFactorsCard from '../pages/account/AuthFactorsCard'

/**
 * Forced enrolment (issue #905, design §7 Stage 2).
 *
 * Past `REQUIRE_STRONG_FACTOR_AFTER` a PIN login on an account with no strong factor mints an
 * `enroll`-scoped session: signed in — the PIN is still a valid bootstrap, nobody is locked out —
 * but able to reach only the enrolment surface. This is that surface's screen.
 *
 * It REPLACES the app rather than covering it, because everything behind it would 403: rendering
 * the pages underneath would just fill the console with refusals and show a broken dashboard.
 *
 * It watches the factors query rather than polling the session: `AuthFactorsCard` invalidates that
 * key when a passkey or authenticator lands, the server promotes the session to `full` in the same
 * request, and re-reading /auth/session is what un-mounts this component.
 */
export default function StrongFactorGate() {
  const { sessionToken, refreshSession, logout } = useAuth()

  const { data } = useQuery({
    queryKey: ['auth-factors', sessionToken],
    queryFn: () =>
      api
        .get(`/user/auth-factors?session_token=${encodeURIComponent(sessionToken!)}`)
        .then((r) => r.data.detail as { has_strong_factor: boolean }),
    enabled: !!sessionToken,
    staleTime: 30 * 1000,
  })

  const enrolled = !!data?.has_strong_factor
  // Once. The re-read updates auth state, which re-renders this component — without the latch a
  // transient failure (or any future change to refreshSession's identity) becomes a request loop.
  const asked = useRef(false)
  useEffect(() => {
    if (!enrolled || asked.current) return
    asked.current = true
    refreshSession().catch(() => {})
  }, [enrolled, refreshSession])

  return (
    <div data-testid="strong-factor-gate" className="max-w-2xl mx-auto space-y-4">
      <div className="border border-blue-200 bg-blue-50 rounded-lg px-4 py-3">
        <h2 className="text-base font-bold text-blue-900">Set up two-factor sign-in to continue</h2>
        <p className="text-sm text-blue-900 mt-1">
          An emailed code on its own no longer protects a LinkedIn account well enough. Add a
          passkey or an authenticator app — it takes a minute, and you only do it once. You are
          signed in; the rest of LEM unlocks as soon as a factor is saved.
        </p>
      </div>

      <AuthFactorsCard />

      <button
        type="button"
        onClick={() => logout()}
        className="text-xs text-gray-500 hover:text-gray-700 font-medium"
      >
        Sign out instead
      </button>
    </div>
  )
}
