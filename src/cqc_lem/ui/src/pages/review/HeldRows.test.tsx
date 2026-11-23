import { describe, expect, it, vi, afterEach, beforeEach } from 'vitest'
import type { ReactNode } from 'react'
import { cleanup, render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import ConnectionRequests from './ConnectionRequests'
import OutreachFunnel from './OutreachFunnel'

// Issue #2623 — a row bounced back to pending by the brand acquisition hold used to look like an
// ordinary draft with a working Approve button. Both review tabs now say why it is held.
const get = vi.fn()

vi.mock('../../api/client', () => ({
  default: { get: (...args: unknown[]) => get(...args), post: vi.fn(), put: vi.fn() },
}))
vi.mock('../../contexts/useAuth', () => ({
  useAuth: () => ({ user: { email: 'test@example.com', userId: 1 }, sessionToken: 'tok' }),
}))
vi.mock('../../utils/analytics', () => ({ maskProps: (className: string) => ({ className }) }))

const HOLD = 'held: brand acquisition outbound is sent by the owner (issue #2376)'

function harness(ui: ReactNode) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  return render(<QueryClientProvider client={client}>{ui}</QueryClientProvider>)
}

beforeEach(() => get.mockReset())
afterEach(() => cleanup())

describe('held pending rows (issue #2623)', () => {
  it('shows the hold reason on a pending connection request', async () => {
    get.mockResolvedValue({ data: { detail: { requests: [{
      id: 1, recipient_profile_url: 'https://www.linkedin.com/in/a', recipient_name: 'A', message: null,
      status: 'pending', created_at: '2026-11-22T16:00:00+00:00', source: 'adjacent_post',
      icp_score: null, reasons: null, failure_reason: HOLD }], total: 1 } } })
    harness(<ConnectionRequests userTimezone="UTC" />)
    expect(await screen.findByText(`Held: ${HOLD}`)).toBeDefined()
  })

  it('shows the hold reason on a pending funnel target', async () => {
    get.mockResolvedValue({ data: { detail: { targets: [{
      id: 2, target_profile_url: 'https://www.linkedin.com/in/b', target_name: 'B', stage: 'connect',
      status: 'pending', context_url: null, draft_text: 'hi', notes: HOLD, updated_at: null }], total: 1 } } })
    harness(<OutreachFunnel />)
    expect(await screen.findByText(`Held: ${HOLD}`)).toBeDefined()
  })

  it('shows nothing extra on a pending funnel target with no note', async () => {
    get.mockResolvedValue({ data: { detail: { targets: [{
      id: 3, target_profile_url: 'https://www.linkedin.com/in/c', target_name: 'C', stage: 'connect',
      status: 'pending', context_url: null, draft_text: 'hi', notes: null, updated_at: null }], total: 1 } } })
    harness(<OutreachFunnel />)
    await screen.findByText('C')
    expect(screen.queryByText(/^Held:/)).toBeNull()
  })
})
