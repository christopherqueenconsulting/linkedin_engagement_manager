import { useQuery } from '@tanstack/react-query'
import api from '../api/client'
import { useAuth } from '../contexts/useAuth'
import type { UserSettings } from '../pages/account/types'

// Whether the account page should word engagement as suggestions (issue #2493). A suggest-mode
// account (every trial, #2367) never has LEM comment, reply, post into groups or message for it, so
// copy describing those lanes as running would be untrue for it.
//
// Same query key and payload as UserPrefsProvider, so the settings page reads /user/settings once.
// Only a stored 'automate' turns full-automation wording on — loading, an error or anything else
// reads as suggest, matching the backend, where an unreadable mode is suggest too.
export type EngagementMode = UserSettings['engagement_mode']

// Shared with all settings readers so they make one `/user/settings` request.
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

// Fail closed: loading, unreadable, or unknown mode keeps credential controls hidden.
export function useSuggestOnly(): boolean {
  return useEngagementMode() !== 'automate'
}
