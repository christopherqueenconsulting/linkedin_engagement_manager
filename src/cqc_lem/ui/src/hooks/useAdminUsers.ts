import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import api from '../api/client'

// Hand-written, like `useAdminFeedback`: every `/api/admin/*` operation is kept out of the
// published OpenAPI schema (#1020), so the generated `schema.ts` has nothing to derive from.

export interface AdminUserSummary {
  id: number
  email: string
  linkedin_email: string | null
  is_admin: boolean
  admin_via_column: boolean
  admin_via_allowlist: boolean
  subscription_status: string | null
  subscription_tier: string | null
  trial_ends_at: string | null
  linkedin_connection_status: string | null
  last_login: string | null
  signed_up_at: string | null
  activated_at: string | null
}

export interface AdminUserDetail extends AdminUserSummary {
  public_uid: string | null
  linkedin_display_name: string | null
  email_verified_at: string | null
  trial_started_at: string | null
  subscription_current_period_end: string | null
  timezone: string | null
  city: string | null
  country: string | null
  locale: string | null
  content_language: string | null
  location_source: string | null
  blog_url: string | null
  sitemap_url: string | null
  company_linked_in_url: string | null
  auto_schedule_posts: boolean
  content_buffer_days: number | null
  content_buffer_max_posts: number | null
  last_login_inactivate_delay: number | null
  avatar_disabled: boolean
  avatar_use_post_image: boolean
  avatar_use_carousel: boolean
  avatar_use_video: boolean
  avatar_use_newsletter: boolean
  avatar_caption_overlay: boolean
  updated_at: string | null
  linkedin_connected_at: string | null
  voice_set_at: string | null
  first_post_approved_at: string | null
  caps_enabled_at: string | null
  max_comments_per_day: number | null
  max_dms_per_day: number | null
  posts_per_week: number | null
  comment_length: string | null
}

export interface AdminUserList {
  items: AdminUserSummary[]
  total: number
  limit: number
  offset: number
}

export interface AdminUserFilters {
  sessionToken: string
  q?: string
  subscriptionStatus?: string
  connectionStatus?: string
  isAdmin?: boolean
  limit?: number
  offset?: number
}

export function useAdminUsers(options: AdminUserFilters) {
  const { sessionToken, q, subscriptionStatus, connectionStatus, isAdmin,
    limit = 25, offset = 0 } = options
  const params: Record<string, string | number | boolean> = {
    session_token: sessionToken, limit, offset,
  }
  if (q) params.q = q
  if (subscriptionStatus) params.subscription_status = subscriptionStatus
  if (connectionStatus) params.linkedin_connection_status = connectionStatus
  if (isAdmin !== undefined) params.is_admin = isAdmin

  return useQuery({
    queryKey: ['admin-users', q, subscriptionStatus, connectionStatus, isAdmin, limit, offset],
    queryFn: () => api.get('/admin/users', { params }).then((r) => r.data.detail as AdminUserList),
    enabled: !!sessionToken,
    staleTime: 30 * 1000,
  })
}

export function useAdminUserDetail(userId: number | null, sessionToken: string) {
  return useQuery({
    queryKey: ['admin-user', userId],
    queryFn: () => api
      .get(`/admin/users/${userId}`, { params: { session_token: sessionToken } })
      .then((r) => r.data.detail as AdminUserDetail),
    enabled: !!sessionToken && userId !== null,
  })
}

export interface RoleChangeResult {
  user_id: number
  is_admin: boolean
  changed: boolean
}

interface RoleChangePayload {
  userId: number
  isAdmin: boolean
  sessionToken: string
}

// The server owns every refusal (self-revoke, the last admin, an allowlist admin, an unreadable
// admin count) — this hook never pre-judges one, so the message the admin reads is the server's.
export function useSetUserAdmin() {
  const queryClient = useQueryClient()
  return useMutation<RoleChangeResult, unknown, RoleChangePayload>({
    mutationFn: ({ userId, isAdmin, sessionToken }: RoleChangePayload) =>
      api
        .post(`/admin/users/${userId}/admin`, { session_token: sessionToken, is_admin: isAdmin })
        .then((r) => r.data.detail),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['admin-users'] })
      void queryClient.invalidateQueries({ queryKey: ['admin-user'] })
    },
  })
}
