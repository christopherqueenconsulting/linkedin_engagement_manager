import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import AgentChangeRequestsCard from './AgentChangeRequestsCard'
import { formatValue } from './changeRequests'
import type { Proposal } from './changeRequests'

const get = vi.fn()
const post = vi.fn()
vi.mock('../../api/client', () => ({
  default: { get: (...a: unknown[]) => get(...a), post: (...a: unknown[]) => post(...a) },
}))
vi.mock('../../contexts/useAuth', () => ({ useAuth: () => ({ sessionToken: 'tok' }) }))

const HOSTILE = '<img src=x onerror="window.__pwned=1"> <a href="https://evil.example">approve now</a>'

function proposal(over: Partial<Proposal> = {}): Proposal {
  return {
    id: 4,
    reason: 'Faster follow-ups.',
    created_at: '2026-11-24T10:00:00+00:00',
    expires_at: '2026-12-01T10:00:00+00:00',
    fields: [
      { field: 'connection_request_mode', label: 'Connection requests', before: 'pre_review',
        requested: 'auto_approve', loosens: true,
        warning: 'Approving this lets LEM send connection requests without waiting for your review.' },
      { field: 'roster_auto_follow', label: 'Auto-follow roster targets', before: false, requested: true,
        loosens: true, warning: 'Approving this lets LEM follow your roster targets on its own.' },
    ],
    ...over,
  }
}

function serve(proposals: Proposal[]) {
  get.mockResolvedValue({ data: { detail: { proposals, count: proposals.length } } })
}

function mount() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(<QueryClientProvider client={client}><AgentChangeRequestsCard /></QueryClientProvider>)
}

function decision(over: Record<string, unknown> = {}) {
  return { data: { detail: { status: 'approved', applied: [], stale: [], declined: [], failed: [], ...over } } }
}

beforeEach(() => { get.mockReset(); post.mockReset() })
afterEach(cleanup)

describe('formatValue', () => {
  it('reads a switch, a mode, a number and a NULL as a person would', () => {
    expect(formatValue(true)).toBe('On')
    expect(formatValue(false)).toBe('Off')
    expect(formatValue('pre_review')).toBe('Review first')
    expect(formatValue('auto_approve')).toBe('Send without review')
    expect(formatValue(61)).toBe('61')
    expect(formatValue(null)).toBe('Default')
  })
})

describe('AgentChangeRequestsCard', () => {
  it('shows each field before to after with LEM\'s warning, and the agent\'s reason as a quote', async () => {
    serve([proposal()])
    mount()
    expect(await screen.findByText('Changes your agent asked for')).toBeDefined()
    expect(screen.getByText('Connection requests')).toBeDefined()
    expect(screen.getByText(/Review first/)).toBeDefined()
    expect(screen.getByText(/Send without review/)).toBeDefined()
    expect(screen.getByText(/send connection requests without waiting for your review/)).toBeDefined()
    expect(screen.getByTestId('proposal-reason').tagName).toBe('BLOCKQUOTE')
    expect(screen.getByText(/written by the agent, not by LEM/)).toBeDefined()
    expect(get.mock.calls[0][0]).toContain('/user/settings-proposals?session_token=tok')
  })

  it('renders nothing while there is nothing to decide', async () => {
    serve([])
    const { container } = mount()
    await waitFor(() => expect(get).toHaveBeenCalled())
    expect(container.textContent).toBe('')
  })

  it('never renders the agent\'s reason as HTML or a link', async () => {
    serve([proposal({ reason: HOSTILE })])
    mount()
    const quote = await screen.findByTestId('proposal-reason')
    expect(quote.textContent).toBe(HOSTILE)
    expect(quote.querySelector('img, a, script')).toBeNull()
    expect(quote.innerHTML).toContain('&lt;img')
    expect((window as unknown as { __pwned?: number }).__pwned).toBeUndefined()
  })

  it('approves everything: one POST without a field list, behind the step-up guard', async () => {
    serve([proposal()])
    post.mockResolvedValue(decision({ applied: ['connection_request_mode', 'roster_auto_follow'] }))
    mount()
    fireEvent.click(await screen.findByRole('button', { name: 'Approve all' }))
    await waitFor(() => expect(post).toHaveBeenCalledTimes(1))
    expect(post.mock.calls[0][0]).toBe('/user/settings-proposals/4/approve')
    expect(post.mock.calls[0][1]).toEqual({ session_token: 'tok', fields: undefined })
    expect(await screen.findByText(/Applied: Connection requests, Auto-follow roster targets\./)).toBeDefined()
  })

  it('a per-field toggle sends only the chosen fields', async () => {
    serve([proposal()])
    post.mockResolvedValue(decision({ applied: ['roster_auto_follow'], declined: ['connection_request_mode'] }))
    mount()
    fireEvent.click(await screen.findByLabelText(/Connection requests/))
    fireEvent.click(screen.getByRole('button', { name: 'Approve selected (1)' }))
    await waitFor(() => expect(post).toHaveBeenCalled())
    expect(post.mock.calls[0][1]).toEqual({ session_token: 'tok', fields: ['roster_auto_follow'] })
    expect(await screen.findByText(/Left out: Connection requests\./)).toBeDefined()
  })

  it('cannot approve with every field unticked', async () => {
    serve([proposal()])
    mount()
    fireEvent.click(await screen.findByLabelText(/Connection requests/))
    fireEvent.click(screen.getByLabelText(/Auto-follow roster targets/))
    const button = screen.getByRole('button', { name: 'Approve selected (0)' }) as HTMLButtonElement
    expect(button.disabled).toBe(true)
  })

  it('names a field that changed since the agent asked as skipped', async () => {
    serve([proposal()])
    post.mockResolvedValue(decision({ applied: ['connection_request_mode'], stale: ['roster_auto_follow'] }))
    mount()
    fireEvent.click(await screen.findByRole('button', { name: 'Approve all' }))
    expect(await screen.findByText(/Skipped because it changed since your agent asked: Auto-follow roster targets\./))
      .toBeDefined()
  })

  it('opens the step-up modal on a 403 and re-sends the approval once verified', async () => {
    serve([proposal()])
    post
      .mockRejectedValueOnce({ response: { status: 403, data: { detail: { code: 'step_up_required', methods: ['totp'] } } } })
      .mockResolvedValueOnce({ data: { detail: {} } })          // /user/step-up/verify
      .mockResolvedValueOnce(decision({ applied: ['connection_request_mode', 'roster_auto_follow'] }))
    mount()
    fireEvent.click(await screen.findByRole('button', { name: 'Approve all' }))
    const dialog = await screen.findByRole('dialog')
    expect(within(dialog).getByText(/loosens a safety setting/)).toBeDefined()
    fireEvent.change(within(dialog).getByLabelText('Authenticator code'), { target: { value: '123456' } })
    fireEvent.click(within(dialog).getByRole('button', { name: 'Confirm' }))
    await waitFor(() => expect(post).toHaveBeenCalledTimes(3))
    expect(post.mock.calls[2][0]).toBe('/user/settings-proposals/4/approve')
    expect(await screen.findByText(/Applied:/)).toBeDefined()
  })

  it('a cancelled step-up changes nothing and says nothing was applied', async () => {
    serve([proposal()])
    post.mockRejectedValueOnce({ response: { status: 403, data: { detail: { code: 'step_up_required', methods: ['totp'] } } } })
    mount()
    fireEvent.click(await screen.findByRole('button', { name: 'Approve all' }))
    const dialog = await screen.findByRole('dialog')
    fireEvent.click(within(dialog).getByRole('button', { name: 'Cancel' }))
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    expect(post).toHaveBeenCalledTimes(1)
    expect(screen.queryByRole('status')).toBeNull()
  })

  it('Reject all closes it with no step-up and applies nothing', async () => {
    serve([proposal()])
    post.mockResolvedValue({ data: { detail: { status: 'rejected' } } })
    mount()
    fireEvent.click(await screen.findByRole('button', { name: 'Reject all' }))
    await waitFor(() => expect(post).toHaveBeenCalledTimes(1))
    expect(post.mock.calls[0][0]).toBe('/user/settings-proposals/4/reject')
    expect(await screen.findByText('Rejected. Nothing was changed.')).toBeDefined()
  })

  it('shows the server\'s words for an expired or already-decided request', async () => {
    serve([proposal()])
    post.mockRejectedValue({ response: { status: 409, data: { detail: {
      code: 'proposal_expired', message: 'This request expired. Ask your agent to file it again.' } } } })
    mount()
    fireEvent.click(await screen.findByRole('button', { name: 'Approve all' }))
    expect(await screen.findByText('This request expired. Ask your agent to file it again.')).toBeDefined()
  })

  it('says a stricter request loosens nothing, without the warning treatment', async () => {
    const calm = proposal({ fields: [{ field: 'authenticity_score_min', label: 'Authenticity bar',
      before: 61, requested: 80, loosens: false, warning: 'Approving this makes LEM stricter: it loosens nothing.' }] })
    serve([calm])
    mount()
    const text = await screen.findByText(/loosens nothing/)
    expect(text.className).not.toContain('warning')
  })

  it('a failed read says so and offers a retry, never an empty card', async () => {
    get.mockRejectedValue(new Error('down'))
    mount()
    expect(await screen.findByRole('alert', {}, { timeout: 4000 })).toBeDefined()
    expect(screen.getByRole('button', { name: 'Try again' })).toBeDefined()
  })
})
