import { describe, expect, it, vi, afterEach, beforeEach } from 'vitest'
import type { ReactNode } from 'react'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import ScheduledDMs from './ScheduledDMs'

// Issue #1528: the reporter's Approve and Cancel buttons "did not work". Both wrote to the server
// and then said nothing at all — a refused write left the row exactly as it was, which from the
// operator's side is indistinguishable from a dead button. Every outcome is now stated.
const get = vi.fn()
const put = vi.fn()

vi.mock('../../api/client', () => ({
  default: {
    get: (...args: unknown[]) => get(...args),
    post: vi.fn(),
    put: (...args: unknown[]) => put(...args),
  },
}))

vi.mock('../../contexts/useAuth', () => ({
  useAuth: () => ({ user: { email: 'test@example.com', userId: 1 }, sessionToken: 'tok' }),
}))

vi.mock('../../utils/analytics', () => ({
  maskProps: (className: string) => ({ className }),
}))

const PENDING_DM = {
  id: 12,
  recipient_profile_url: 'https://www.linkedin.com/in/jane',
  recipient_name: 'Jane Doe',
  message: 'Here is that audit checklist.',
  scheduled_time: '2026-07-28T13:00:00+00:00',
  status: 'pending',
  source: 'artifact',
}

function harness(ui: ReactNode) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  return render(<QueryClientProvider client={client}>{ui}</QueryClientProvider>)
}

beforeEach(() => {
  get.mockReset()
  put.mockReset()
  get.mockResolvedValue({ data: { detail: { dms: [PENDING_DM], total: 1 } } })
})

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

const render_ = () => harness(<ScheduledDMs userTimezone="UTC" timezoneResolved />)
const approve = () => screen.getByRole('button', { name: 'Approve' })
const cancel = () => screen.getByRole('button', { name: 'Cancel' })

describe('ScheduledDMs approve/cancel (issue #1528)', () => {
  it('says what an approve did, not just that it was clicked', async () => {
    put.mockResolvedValue({ data: { detail: 'Scheduled DM updated' } })
    render_()
    await waitFor(() => expect(approve()).toBeDefined())

    fireEvent.click(approve())

    await waitFor(() =>
      expect(put).toHaveBeenCalledWith('/dm', { session_token: 'tok', dm_id: 12, action: 'approve' }))
    expect(await screen.findByText(/Approved — it will send at its scheduled time\./)).toBeDefined()
  })

  it('says what a cancel did', async () => {
    put.mockResolvedValue({ data: { detail: 'Scheduled DM updated' } })
    render_()
    await waitFor(() => expect(cancel()).toBeDefined())

    fireEvent.click(cancel())

    await waitFor(() =>
      expect(put).toHaveBeenCalledWith('/dm', { session_token: 'tok', dm_id: 12, action: 'cancel' }))
    expect(await screen.findByText(/Canceled — this DM will not be sent\./)).toBeDefined()
  })

  it('surfaces a refused approve instead of leaving the row silently unchanged', async () => {
    put.mockRejectedValue({ response: { data: { detail: 'Scheduled DM not found' } } })
    render_()
    await waitFor(() => expect(approve()).toBeDefined())

    fireEvent.click(approve())

    // The server's own reason, not a generic shrug — "not found" is something the operator acts on.
    expect(await screen.findByText('Scheduled DM not found')).toBeDefined()
  })

  it('falls back to a plain message when the failure carries no detail', async () => {
    put.mockRejectedValue(new Error('Network Error'))
    render_()
    await waitFor(() => expect(cancel()).toBeDefined())

    fireEvent.click(cancel())

    expect(await screen.findByText('Could not cancel this DM — try again.')).toBeDefined()
  })
})
