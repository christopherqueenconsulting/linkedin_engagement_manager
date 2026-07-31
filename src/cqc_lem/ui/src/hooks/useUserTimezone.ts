import { useQuery } from '@tanstack/react-query'
import api from '../api/client'
import { useAuth } from '../contexts/AuthContext'

export interface UserTimezoneState {
  timezone: string
  // True only when `timezone` is the zone the USER configured (Account -> Login Location).
  // While the request is in flight, disabled (no session token yet) or failed, `timezone` is the
  // BROWSER's zone — a guess. Displaying a value in the wrong zone for a moment is cosmetic;
  // CONVERTING a wall clock the user typed against it is not: the instant is stored, and the post
  // fires hours away from what they picked (docs/timezone-contract.md §4). Every surface that
  // writes a scheduled time must hold off until this is true.
  isResolved: boolean
}

export function useUserTimezoneState(): UserTimezoneState {
  const { sessionToken } = useAuth()

  const { data } = useQuery({
    queryKey: ['user-timezone', sessionToken],
    queryFn: () =>
      api
        .get(`/user/timezone?session_token=${encodeURIComponent(sessionToken!)}`)
        .then((r) => r.data.detail as { timezone: string }),
    enabled: !!sessionToken,
    staleTime: 5 * 60 * 1000,
    // Scheduling holds until this answers, so a transient blip must not leave the pickers locked.
    // Pinned rather than inherited: react-query's own default is 3, so the earlier `retry: 2` would
    // have made a blip MORE likely to lock the pickers, not less.
    retry: 3,
  })

  const resolved = data?.timezone
  return {
    timezone: resolved ?? Intl.DateTimeFormat().resolvedOptions().timeZone,
    isResolved: !!resolved,
  }
}

// Display-only callers: a browser-zone fallback for the few milliseconds before the real zone
// arrives is fine. Anything that CONVERTS user input must use useUserTimezoneState instead.
export function useUserTimezone(): string {
  return useUserTimezoneState().timezone
}
