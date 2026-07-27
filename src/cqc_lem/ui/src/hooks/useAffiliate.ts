import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import api from '../api/client'
import { useAuth } from '../contexts/AuthContext'

// Affiliate / ambassador program (issue #737). TWO independent toggles, and the types keep them
// apart the same way the backend does: `enrolled` is (A) affiliate status (default on, one click
// out) and `promo_content_opt_in` is (B) whether LEM may publish promotion from the user's own
// LinkedIn account (default off, and un-settable without recorded consent).
export interface AffiliateState {
  program_enabled: boolean
  eligible: boolean
  status: 'enrolled' | 'opted_out' | 'ineligible'
  enrolled: boolean
  referral_code: string
  referral_url: string
  notice_seen_at: string | null
  referrals: { pending: number; converted: number; rejected: number }
  days_earned: number
  days_from_referrals: number
  max_reward_days: number
  bonus_days: number
  referral_bonus_days: number
  standard_trial_days: number
  promo_content_opt_in: boolean
  promo_consent_at: string | null
  promo_consent_version: string | null
  promo_content_allowed: boolean
  disclosure_text: string
  // Present only on the response to a status change — what the flip actually did to the trial.
  reward_days?: number
  trial_ends_at?: string | null
}

const KEY = 'affiliate'

export function useAffiliate() {
  const { sessionToken } = useAuth()
  return useQuery({
    queryKey: [KEY, sessionToken],
    queryFn: () =>
      api
        .get(`/user/affiliate?session_token=${encodeURIComponent(sessionToken!)}`)
        .then((r) => r.data.detail as AffiliateState),
    enabled: !!sessionToken,
    staleTime: 60 * 1000,
  })
}

export function useSetAffiliateStatus() {
  const { sessionToken } = useAuth()
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (enrolled: boolean) =>
      api
        .post('/user/affiliate/status', { session_token: sessionToken, enrolled })
        .then((r) => r.data.detail as AffiliateState),
    onSuccess: (data) => {
      // Seed the cache from the response rather than only invalidating: the new trial end date is
      // what the confirmation copy reads, and a refetch would show it a beat late.
      queryClient.setQueryData([KEY, sessionToken], data)
      queryClient.invalidateQueries({ queryKey: [KEY, sessionToken] })
    },
  })
}

export function useSetAffiliatePromoConsent() {
  const { sessionToken } = useAuth()
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (enabled: boolean) =>
      api
        .post('/user/affiliate/promo-consent', {
          session_token: sessionToken,
          enabled,
          // The server refuses an enable without this; withdrawing consent is never gated.
          consent_acknowledged: enabled,
        })
        .then((r) => r.data.detail as AffiliateState),
    onSuccess: (data) => queryClient.setQueryData([KEY, sessionToken], data),
  })
}

export function useAcknowledgeAffiliateNotice() {
  const { sessionToken } = useAuth()
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: () =>
      api
        .post('/user/affiliate/notice', { session_token: sessionToken })
        .then((r) => r.data.detail as AffiliateState),
    onSuccess: (data) => queryClient.setQueryData([KEY, sessionToken], data),
  })
}
