import { describe as suite, expect, it, vi, afterEach, beforeEach } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import AgentActivityCard from './AgentActivityCard'
import { describe } from './agentActivity'
import type { McpActivityEvent } from './agentActivity'

const get = vi.fn()
vi.mock('../../api/client', () => ({ default: { get: (...a: unknown[]) => get(...a) } }))
vi.mock('../../contexts/useAuth', () => ({ useAuth: () => ({ sessionToken: 'tok' }) }))

function ev(over: Partial<McpActivityEvent>): McpActivityEvent {
  return { event: 'mcp_write', success: true, client: null, details: null,
    created_at: '2026-11-24T03:00:00+00:00', ...over }
}

function mount() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(<QueryClientProvider client={client}><AgentActivityCard /></QueryClientProvider>)
}

beforeEach(() => get.mockReset())
afterEach(cleanup)

suite('describe', () => {
  it('reads a write per tool: what it did, and to what', () => {
    expect(describe(ev({ details: { tool: 'lem_queue_dm', target_id: 5 } })))
      .toBe('Queued for your review: DM #5')
    expect(describe(ev({ details: { tool: 'lem_approve_item', target_id: 9 } })))
      .toBe('Approved: outreach item #9')
    expect(describe(ev({ details: { tool: 'lem_update_lead', target_id: 2 } }))).toBe('Updated: lead #2')
    expect(describe(ev({ details: { tool: 'lem_edit_catchup_draft', target_id: 3 } })))
      .toBe('Edited, pending your review: catch-up draft #3')
    expect(describe(ev({ details: { tool: 'lem_new_tool' } }))).toBe('Agent change: lem_new_tool')
    expect(describe(ev({ details: null }))).toBe('Agent change')
  })

  it('reads a change request as its field names, never a value or the agent\'s reason', () => {
    const row = { tool: 'lem_request_settings_change', fields: ['roster_auto_follow'], proposal_id: 3 }
    expect(describe(ev({ event: 'mcp_settings_proposed', details: row })))
      .toBe('Asked you to approve a settings change: roster_auto_follow')
    expect(describe(ev({ event: 'mcp_settings_approved', details: { proposal_id: 3, applied: ['tone'] } })))
      .toBe('You approved a settings request: tone')
    expect(describe(ev({ event: 'mcp_settings_rejected', details: { proposal_id: 3, fields: ['a', 'b'] } })))
      .toBe('You rejected a settings request: a, b')
    expect(describe(ev({ event: 'mcp_settings_rejected', details: null }))).toBe('You rejected a settings request')
  })

  it('never renders a non-write event\'s tool as its object', () => {
    expect(describe(ev({ event: 'mcp_access', details: { tool: 'lem_queue_dm', scope: 'mcp' } })))
      .toBe('Connected')
    expect(describe(ev({ event: 'mcp_approval', details: { via: 'action', tool: 'lem_approve_item' } })))
      .toBe('Approved (internal/sandbox account)')
    expect(describe(ev({ event: 'mcp_unknown_event' }))).toBe('mcp_unknown_event')
  })

  it('reads a settings change as its field names, never a value', () => {
    const row = { tool: 'lem_update_settings', fields: ['tone', 'include_topics'], scope: 'mcp' }
    expect(describe(ev({ event: 'mcp_settings', details: row })))
      .toBe('Changed settings: tone, include_topics')
    expect(describe(ev({ event: 'mcp_settings', details: { ...row, tool: 'lem_undo_settings_change' } })))
      .toBe('Undid a settings change: tone, include_topics')
    expect(describe(ev({ event: 'mcp_settings', details: null }))).toBe('Changed settings')
  })
})

suite('AgentActivityCard', () => {
  it('lists the events from the activity route', async () => {
    get.mockResolvedValue({ data: { detail: { events: [
      ev({ details: { tool: 'lem_draft_post', target_id: 33 }, client: 'claude-code' }),
      ev({ event: 'mcp_access' }),
    ] } } })
    mount()
    expect(await screen.findByText('Queued for your review: post draft #33')).toBeDefined()
    expect(screen.getAllByTestId('mcp-activity-row')).toHaveLength(2)
    expect(get.mock.calls[0][0]).toContain('/user/mcp-token/activity?session_token=tok')
  })

  it('says when there is nothing yet', async () => {
    get.mockResolvedValue({ data: { detail: { events: [] } } })
    mount()
    expect(await screen.findByText('No agent activity yet.')).toBeDefined()
  })

  it('offers a retry when the read fails, never an empty list', async () => {
    // The card retries once on its own, so both attempts fail before the alert shows.
    get.mockRejectedValue(new Error('503'))
    mount()
    expect((await screen.findByRole('alert', {}, { timeout: 5000 })).textContent)
      .toMatch(/Could not load agent activity/)
    expect(screen.queryByText('No agent activity yet.')).toBeNull()
    get.mockResolvedValue({ data: { detail: { events: [ev({ event: 'mcp_access' })] } } })
    fireEvent.click(screen.getByRole('button', { name: 'Try again' }))
    await waitFor(() => expect(screen.getByText('Connected')).toBeDefined())
  })
})
