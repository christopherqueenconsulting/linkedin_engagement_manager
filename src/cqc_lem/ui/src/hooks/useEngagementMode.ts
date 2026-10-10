import { useQuery } from '@tanstack/react-query'
import api from '../api/client'
import { useAuth } from '../contexts/useAuth'
import type { UserSettings } from '../pages/account/types'

export type EngagementMode = UserSettings['engagement_mode']

// The account's engagement mode as GET /user/settings reports it (issue #2367). The server resolves
// it fail-closed, so 'automate' here means a readable, non-trial automate account. Shares the
// 'user-settings' cache entry with the other readers of that endpoint. `undefined` while loading
// or when the read failed — callers that gate credential UI treat that as "not automate".
export function useEngagementMode(): EngagementMode | undefined {
  const { sessionToken } = useAuth()
  const { data } = useQuery({
    queryKey: ['user-settings', sessionToken],
    queryFn: () =>
      api
        .get(`/user/settings?session_token=${encodeURIComponent(sessionToken!)}`)
        .then((r) => r.data.detail as UserSettings),
    enabled: !!sessionToken,
    staleTime: 60 * 1000,
  })
  return data?.engagement_mode
}
