import { useQuery } from '@tanstack/react-query'
import api from '../api/client'

export interface AdminFeedbackItem {
  id: number
  user_id: number | null
  email: string | null
  is_admin_reporter: boolean
  source: string
  type_hint: string | null
  body: string
  context_json: string | object | null
  status: string
  cluster_id: number | null
  github_issue_number: number | null
  reviewed_by: number | null
  reviewed_at: string | null
  created_at: string
}

export interface AdminFeedbackList {
  items: AdminFeedbackItem[]
  limit: number
  offset: number
}

interface UseAdminFeedbackOptions {
  sessionToken: string
  status?: string
  source?: string
  limit?: number
  offset?: number
}

export function useAdminFeedback(options: UseAdminFeedbackOptions) {
  const { sessionToken, status, source, limit = 50, offset = 0 } = options
  const params: Record<string, string | number> = { session_token: sessionToken, limit, offset }
  if (status) params.status = status
  if (source) params.source = source

  return useQuery({
    queryKey: ['admin-feedback', status, source, limit, offset],
    queryFn: () =>
      api
        .get('/admin/feedback', { params })
        .then((r) => (r.data.detail as AdminFeedbackList)),
    enabled: !!sessionToken,
    staleTime: 30 * 1000,
  })
}
