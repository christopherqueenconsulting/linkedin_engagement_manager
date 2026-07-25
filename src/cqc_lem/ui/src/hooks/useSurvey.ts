import { useQuery } from '@tanstack/react-query'
import api from '../api/client'
import { useAuth } from '../contexts/AuthContext'

export interface SurveyPrompt {
  key: string
  source: 'nps' | 'review'
  headline: string
  body: string
  cta_label: string
}

export interface SurveySnapshot {
  survey: SurveyPrompt | null
  review_submitted: boolean
  nps_min: number
  nps_max: number
  review_min_rating: number
  review_max_rating: number
}

// The survey to ask right now — day-3 NPS, trial T-3d NPS, or the review that unlocks the
// extended trial (issue #501). Null `survey` means there is nothing to ask.
export function useSurvey() {
  const { sessionToken } = useAuth()
  return useQuery({
    queryKey: ['survey', sessionToken],
    queryFn: () =>
      api
        .get(`/user/survey?session_token=${encodeURIComponent(sessionToken!)}`)
        .then((r) => r.data.detail as SurveySnapshot),
    enabled: !!sessionToken,
    staleTime: 5 * 60 * 1000,
  })
}
