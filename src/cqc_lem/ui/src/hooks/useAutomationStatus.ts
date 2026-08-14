import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import api from '../api/client'
import { useAuth } from '../contexts/useAuth'

export interface SuppressionSignal {
  name: string
  status: 'ok' | 'watch' | 'tripped' | 'unknown'
  reason: string
  metric?: string | null
  baseline?: number | null
  max_drop?: number | null
}

export interface SuppressionVerdict {
  status: 'ok' | 'watch' | 'tripped' | 'unknown'
  tripped: boolean
  reason: string
  signals: SuppressionSignal[]
}

export interface AutomationStatus {
  enabled: boolean
  tripped: boolean
  trip: { reason: string; tripped_at: string | null } | null
  current: SuppressionVerdict
  recovered: boolean
  engagement_paused: boolean
  pause_by_tripwire: boolean
  pause_remaining_s: number
  breaker_remaining_s: number
}

// Suppression-tripwire state (issue #629): whether LEM stopped its own engagement automation
// because the account's reach collapsed, and whether the signals have since recovered.
export function useAutomationStatus() {
  const { sessionToken } = useAuth()
  return useQuery({
    queryKey: ['automation-status', sessionToken],
    queryFn: () =>
      api
        .get(`/user/automation-status?session_token=${encodeURIComponent(sessionToken!)}`)
        .then((r) => r.data.detail as AutomationStatus),
    enabled: !!sessionToken,
    staleTime: 60 * 1000,
  })
}

// The manual re-enable path. The tripwire never resumes on its own — this is the only way back.
export function useResumeAutomation() {
  const { sessionToken } = useAuth()
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: () =>
      api
        .post('/user/automation-resume', { session_token: sessionToken })
        .then((r) => r.data.detail as AutomationStatus),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['automation-status', sessionToken] })
    },
  })
}
