import { useQuery } from '@tanstack/react-query'
import api from '../api/client'

// Hand-written, like `useAdminUsers`: every `/api/admin/*` operation is kept out of the published
// OpenAPI schema (#1020), so the generated `schema.ts` has nothing to derive from.

export interface AdminAuditLogEntry {
  id: number
  user_id: number | null
  email: string | null
  event: string
  success: boolean
  user_agent: string | null
  session_id: number | null
  details: Record<string, unknown> | null
  created_at: string | null
}

export interface AdminAuditLogPage {
  items: AdminAuditLogEntry[]
  total: number
  limit: number
  offset: number
}

export interface AdminAuditLogFilters {
  sessionToken: string
  userId?: number
  limit?: number
  offset?: number
}

// Never requests or reads `ip_hash` — the server does not return it (issue #1603, §2 of
// docs/admin-user-management.md): it is stored for forensics, not for a screen.
export function useAdminAuditLog(options: AdminAuditLogFilters) {
  const { sessionToken, userId, limit = 50, offset = 0 } = options
  const params: Record<string, string | number> = { session_token: sessionToken, limit, offset }
  if (userId !== undefined) params.user_id = userId

  return useQuery({
    queryKey: ['admin-audit-log', userId, limit, offset],
    queryFn: () => api.get('/admin/audit-log', { params }).then((r) => r.data.detail as AdminAuditLogPage),
    enabled: !!sessionToken,
    staleTime: 30 * 1000,
  })
}
