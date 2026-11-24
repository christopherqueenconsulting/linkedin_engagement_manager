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

// Events that are not a write: they read as themselves. Their `details` may name a tool (the gate
// records the first tool of the day on `mcp_access`), but that tool is NOT the thing that happened,
// so it is never rendered as the event's object.
const EVENT_LABELS: Record<string, string> = {
  mcp_approval: 'Approved (internal/sandbox account)',
  mcp_access: 'Connected',
  mcp_token_minted: 'Connection created',
  mcp_token_rotated: 'Connection rotated',
  mcp_token_exposed: 'Token exposed in a URL and revoked',
  mcp_kill_toggled: 'Kill switch changed',
  mcp_sessions_revoked: 'All connections revoked',
}

// An `mcp_write` row reads per tool: what it did, and to what.
const WRITE_LABELS: Record<string, { verb: string; object: string }> = {
  lem_draft_post: { verb: 'Queued for your review', object: 'post draft' },
  lem_revise_post_draft: { verb: 'Edited, pending your review', object: 'post draft' },
  lem_queue_dm: { verb: 'Queued for your review', object: 'DM' },
  lem_queue_connection_request: { verb: 'Queued for your review', object: 'connection request' },
  lem_add_outreach_target: { verb: 'Queued for your review', object: 'outreach target' },
  lem_edit_lead_signal_draft: { verb: 'Edited, pending your review', object: 'lead reply draft' },
  lem_edit_catchup_draft: { verb: 'Edited, pending your review', object: 'catch-up draft' },
  lem_update_lead: { verb: 'Updated', object: 'lead' },
  lem_approve_item: { verb: 'Approved', object: 'outreach item' },
}

function withTarget(text: string, target: unknown): string {
  return target !== undefined && target !== null ? `${text} #${String(target)}` : text
}

/** One row's text. Unknown events fall back to their raw name; an unknown write tool to its name. */
export function describe(e: McpActivityEvent): string {
  if (e.event === 'mcp_write') {
    const tool = typeof e.details?.tool === 'string' ? e.details.tool : null
    if (!tool) return 'Agent change'
    const label = WRITE_LABELS[tool]
    const text = label ? `${label.verb}: ${label.object}` : `Agent change: ${tool}`
    return withTarget(text, e.details?.target_id)
  }
  return EVENT_LABELS[e.event] ?? e.event
}
