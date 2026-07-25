import { useQuery } from '@tanstack/react-query'
import api from '../api/client'
import { useAuth } from '../contexts/AuthContext'

export interface OnboardingStep {
  key: string
  label: string
  hint: string
  path: string
  ok: boolean
  completed_at: string | null
}

export interface OnboardingNudge {
  key: string
  headline: string
  body: string
  cta_label: string
  cta_path: string
}

export interface Onboarding {
  activated: boolean
  started_at: string | null
  steps: OnboardingStep[]
  nudge: OnboardingNudge | null
}

// The activation checklist (issue #500). Reading it also advances the server-side state,
// so the PostHog activation funnel records each step as soon as the user finishes it.
export function useOnboarding() {
  const { sessionToken } = useAuth()
  return useQuery({
    queryKey: ['onboarding', sessionToken],
    queryFn: () =>
      api
        .get(`/user/onboarding?session_token=${encodeURIComponent(sessionToken!)}`)
        .then((r) => r.data.detail as Onboarding),
    enabled: !!sessionToken,
    staleTime: 60 * 1000,
  })
}
