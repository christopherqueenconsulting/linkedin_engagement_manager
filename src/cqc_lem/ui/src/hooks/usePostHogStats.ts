import { useQuery } from '@tanstack/react-query'
import api from '../api/client'
import { useAuth } from '../contexts/AuthContext'

export interface PostHogStatsRow {
  [column: string]: string | number | boolean | null
}

export interface PostHogStatsPanelResult {
  available: boolean
  rows: PostHogStatsRow[]
}

export interface PostHogStats {
  posts_engagement: PostHogStatsPanelResult
  comment_activity: PostHogStatsPanelResult
  llm_cost_by_feature: PostHogStatsPanelResult
}

// The in-SPA "your stats" panel (issue #654), served by PostHog HogQL Endpoints via a server-side
// proxy — no PostHog key ever reaches the browser. A missing endpoint/key degrades per-panel
// (`available: false`), so this never blocks or errors the Dashboard.
export function usePostHogStats() {
  const { sessionToken } = useAuth()
  return useQuery({
    queryKey: ['posthog-stats', sessionToken],
    queryFn: () =>
      api
        .get(`/user/posthog-stats?session_token=${encodeURIComponent(sessionToken!)}`)
        .then((r) => r.data.detail as PostHogStats),
    enabled: !!sessionToken,
    staleTime: 5 * 60 * 1000,
    retry: 1,
  })
}
