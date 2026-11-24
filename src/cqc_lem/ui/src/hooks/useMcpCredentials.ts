import { useQuery, useQueryClient } from '@tanstack/react-query'
import api from '../api/client'
import type { components } from '../api/schema'
import type { ApiEnvelope } from '../api/types'
import { useAuth } from '../contexts/useAuth'

// The MCP token surface (issue #2687; the routes are P2, #2685, in api/routers/mcp_tokens.py).
//
// Request bodies come from the GENERATED schema. The response `detail` payloads are spelled here
// because the routes declare `ResponseModel[dict[str, Any]]`, so the schema has nothing narrower
// than an open object for them. TODO(#2685): drop these three shapes for `GetDetail` / `ApiDetail`
// once the routes declare typed response models.

type Schemas = components['schemas']

export type McpMintRequest = Omit<Schemas['McpTokenRequest'], 'session_token'>
export type McpRotateRequest = Omit<Schemas['McpTokenRotateRequest'], 'session_token'>

export type McpScope = 'mcp' | 'mcp_outreach'

export interface McpTokenRow {
  session_id: number
  label: string | null
  scope: McpScope | string
  created_at: string | null
  last_seen_at: string | null
  expires_at: string | null
  revoked: boolean
  expired: boolean
  /** null = Redis could not be read: unknown, which is not the same thing as 0 calls. */
  calls_7d: number | null
  last_client_name: string | null
}

export interface McpInfo {
  enabled: boolean
  /** null when the server has no public base URL configured: there is nothing to connect to. */
  mcp_url: string | null
  tokens: McpTokenRow[]
}

/** The ONLY response that carries a token. It is returned to the caller and never cached. */
export interface McpMintResponse {
  session_token: string
  session_id: number
  mcp_url: string | null
  expires_at: string | null
  label: string
  scope: McpScope | string
  ttl_days: number
}

export const MCP_INFO_KEY = 'mcp-info'

/** The label is nullable server-side; a row is never shown nameless. */
export function rowLabel(row: Pick<McpTokenRow, 'label'>): string {
  return row.label?.trim() || 'Agent connection'
}

/**
 * The token-free half lives in react-query (`GET /user/mcp-token/info`); the token-bearing half
 * does NOT. `mint` and `rotate` are plain async functions, not `useMutation`s, on purpose: a
 * mutation's `data` sits in the MutationCache, so a token returned through one would outlive the
 * component that was meant to be its only holder. The caller keeps the response in component state.
 */
export function useMcpCredentials() {
  const { sessionToken } = useAuth()
  const queryClient = useQueryClient()

  const info = useQuery({
    queryKey: [MCP_INFO_KEY, sessionToken],
    queryFn: () =>
      api
        .get<ApiEnvelope<McpInfo>>(`/user/mcp-token/info?session_token=${encodeURIComponent(sessionToken!)}`)
        .then((r) => r.data.detail),
    enabled: !!sessionToken,
    staleTime: 30 * 1000,
    retry: 1,
  })

  const refresh = () => queryClient.invalidateQueries({ queryKey: [MCP_INFO_KEY] })

  async function mint(body: McpMintRequest): Promise<McpMintResponse> {
    const payload: Schemas['McpTokenRequest'] = { session_token: sessionToken, ...body }
    const r = await api.post<ApiEnvelope<McpMintResponse>>('/user/mcp-token', payload)
    return r.data.detail
  }

  async function rotate(sessionId: number): Promise<McpMintResponse> {
    const payload: Schemas['McpTokenRotateRequest'] = { session_token: sessionToken, session_id: sessionId }
    const r = await api.post<ApiEnvelope<McpMintResponse>>('/user/mcp-token/rotate', payload)
    return r.data.detail
  }

  // The same route and body SecurityCard signs a device out with.
  async function revoke(sessionId: number): Promise<void> {
    await api.post('/user/sessions/revoke', { session_token: sessionToken, session_id: sessionId })
  }

  return { info, refresh, mint, rotate, revoke }
}

/** The server's `code` on a refusal, whether it came as `{detail: {code}}` or `{detail: 'code'}`. */
export function errorCode(error: unknown): string | null {
  const detail = (error as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail
  if (typeof detail === 'string') return detail
  if (detail && typeof detail === 'object' && typeof (detail as { code?: unknown }).code === 'string') {
    return (detail as { code: string }).code
  }
  return null
}

export function errorStatus(error: unknown): number | null {
  const status = (error as { response?: { status?: unknown } })?.response?.status
  return typeof status === 'number' ? status : null
}
