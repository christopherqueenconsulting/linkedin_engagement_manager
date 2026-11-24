// Pure helpers for the Agents tab's activity card (MCP W2): the row shape of
// `GET /user/mcp-token/activity` and how one row reads. Kept out of the component file so it
// exports only components (react-refresh).

export interface McpActivityEvent {
  event: string
  success: boolean
  client: string | null
  details: Record<string, unknown> | null
  created_at: string | null
}

export const MCP_ACTIVITY_KEY = 'mcp-activity'

const EVENT_LABELS: Record<string, string> = {
  mcp_write: 'Queued for your review',
  mcp_approval: 'Approved (internal/sandbox account)',
  mcp_access: 'Connected',
  mcp_token_minted: 'Connection created',
  mcp_token_rotated: 'Connection rotated',
  mcp_token_exposed: 'Token exposed in a URL and revoked',
  mcp_kill_toggled: 'Kill switch changed',
  mcp_sessions_revoked: 'All connections revoked',
}

const TOOL_LABELS: Record<string, string> = {
  lem_draft_post: 'post draft',
  lem_revise_post_draft: 'post draft revision',
  lem_queue_dm: 'DM',
  lem_queue_connection_request: 'connection request',
  lem_add_outreach_target: 'outreach target',
  lem_edit_lead_signal_draft: 'lead reply draft',
  lem_edit_catchup_draft: 'catch-up draft',
  lem_update_lead: 'lead update',
  lem_approve_item: 'approval',
}

/** One row's text: what happened, and to what. Unknown events fall back to their raw name. */
export function describe(e: McpActivityEvent): string {
  const base = EVENT_LABELS[e.event] ?? e.event
  const tool = typeof e.details?.tool === 'string' ? e.details.tool : null
  const target = e.details?.target_id
  const what = tool ? TOOL_LABELS[tool] ?? tool : null
  if (!what) return base
  return `${base}: ${what}${target !== undefined && target !== null ? ` #${String(target)}` : ''}`
}
