import { useQuery } from '@tanstack/react-query'
import api from '../api/client'
import { useAuth } from '../contexts/AuthContext'

export interface ShippedNotice {
  id: number
  issue_number: number
  changelog_line: string
  shipped_at: string | null
}

export interface ShippedSnapshot {
  notices: ShippedNotice[]
  changelog: Omit<ShippedNotice, 'id'>[]
}

// "You asked, we shipped" notices for fixes this user reported (issue #502). The server only
// returns a notice once the reporter has had the fix long enough to judge it, which is what
// schedules the micro-CSAT the notice carries.
export function useShippedNotices() {
  const { sessionToken } = useAuth()
  return useQuery({
    queryKey: ['shipped-notices', sessionToken],
    queryFn: () =>
      api
        .get(`/user/shipped?session_token=${encodeURIComponent(sessionToken!)}`)
        .then((r) => r.data.detail as ShippedSnapshot),
    enabled: !!sessionToken,
    staleTime: 5 * 60 * 1000,
  })
}
