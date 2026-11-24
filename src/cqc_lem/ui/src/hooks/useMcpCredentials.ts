import { useQuery, useQueryClient } from '@tanstack/react-query'
import api from '../api/client'
import { useAuth } from '../contexts/useAuth'

// The MCP token surface (issue #2687; the routes are P2, #2685 — docs/agent-mcp-plan.md §3.5).
//
// TODO(#2685): switch these to the generated `components['schemas'][…]` types in api/schema.ts once
// P2's routes are in openapi.json. They are narrow LOCAL types only because this PR was built in
// parallel with the backend, and schema.ts is regenerated, never hand-edited.

export type McpScope = 'mcp' | 'mcp_outreach'

export interface McpTokenRow {
  session_id: number
  label: string
  scope: McpScope | string
  created_at: string | null
  last_seen_at: string | null
  expires_at: string | null
  /** null = not counted, which is not the same thing as 0 calls. */
  calls_7d: number | null
  last_client_name: string | null
}

export interface McpInfo {
  enabled: boolean
  mcp_url: string
  tokens: McpTokenRow[]
}

export interface McpMintRequest {
  label: string
  ttl_days: 7 | 30 | 90
  outreach: boolean
}

/** The ONLY response that carries a token. It is returned to the caller and never cached. */
export interface McpMintResponse {
  session_token: string
  session_id: number
  mcp_url: string
  expires_at: string | null
  label: string
  scope: McpScope | string
}

export const MCP_INFO_KEY = 'mcp-info'

/**
 * The token-free half lives in react-query (`GET /user/mcp-info`); the token-bearing half does
 * NOT. `mint` and `rotate` are plain async functions, not `useMutation`s, on purpose: a mutation's
 * `data` sits in the MutationCache, so a token returned through one would outlive the component
 * that was meant to be its only holder. The caller keeps the response in component state.
 */
export function useMcpCredentials() {
  const { sessionToken } = useAuth()
  const queryClient = useQueryClient()

  const info = useQuery({
    queryKey: [MCP_INFO_KEY, sessionToken],
    queryFn: () =>
      api
        .get(`/user/mcp-info?session_token=${encodeURIComponent(sessionToken!)}`)
        .then((r) => r.data.detail as McpInfo),
    enabled: !!sessionToken,
    staleTime: 30 * 1000,
    retry: 1,
  })

  const refresh = () => queryClient.invalidateQueries({ queryKey: [MCP_INFO_KEY] })

  async function mint(body: McpMintRequest): Promise<McpMintResponse> {
    const r = await api.post('/user/mcp-token', { session_token: sessionToken, ...body })
    return r.data.detail as McpMintResponse
  }

  async function rotate(sessionId: number): Promise<McpMintResponse> {
    const r = await api.post('/user/mcp-token/rotate', { session_token: sessionToken, session_id: sessionId })
    return r.data.detail as McpMintResponse
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
