import { useQuery } from '@tanstack/react-query'
import api from '../api/client'
import { useAuth } from '../contexts/useAuth'

export interface ReadinessItem {
  key: string
  label: string
  ok: boolean
  required: boolean
  hint: string | null
}

export interface AccountReadiness {
  ready: boolean
  items: ReadinessItem[]
  // True when the account's ONLY engagement login is a stored LinkedIn password (issue #745):
  // the cue to prompt for a session cookie so the password can be deleted rather than kept.
  cookie_migration_needed?: boolean
}

// Reports whether the account has everything the automation needs (LinkedIn OAuth,
// a session cookie or password, an active plan; location optional).
export function useAccountReadiness() {
  const { sessionToken } = useAuth()
  return useQuery({
    queryKey: ['account-readiness', sessionToken],
    queryFn: () =>
      api
        .get(`/user/account-readiness?session_token=${encodeURIComponent(sessionToken!)}`)
        .then((r) => r.data.detail as AccountReadiness),
    enabled: !!sessionToken,
    staleTime: 60 * 1000,
  })
}
