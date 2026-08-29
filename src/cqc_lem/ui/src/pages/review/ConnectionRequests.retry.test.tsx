import { describe, expect, it, vi, afterEach, beforeEach } from 'vitest'
import type { ReactNode } from 'react'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import ConnectionRequests from './ConnectionRequests'

// Issue #1735 — the reporter could not tell whether a failed connection request would ever be
// retried, or direct one themselves. A `failed` row now gets an explicit Retry action instead of
// dead-ending with no controls at all.
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

const FAILED_REQUEST = {
  id: 9,
  recipient_profile_url: 'https://www.linkedin.com/in/jane',
  recipient_name: 'Jane Doe',
  message: null,
  status: 'failed',
  created_at: '2026-07-28T13:00:00+00:00',
  source: null,
  icp_score: null,
  reasons: null,
  failure_reason: 'Already connected',
}

const PENDING_REQUEST = { ...FAILED_REQUEST, id: 10, status: 'pending', failure_reason: null }

function harness(ui: ReactNode) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  return render(<QueryClientProvider client={client}>{ui}</QueryClientProvider>)
}

beforeEach(() => {
  get.mockReset()
  put.mockReset()
})

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

const render_ = () => harness(<ConnectionRequests userTimezone="UTC" />)

describe('ConnectionRequests retry (issue #1735)', () => {
  it('offers a Retry action on a failed request, and explains it is manual', async () => {
    get.mockResolvedValue({ data: { detail: { requests: [FAILED_REQUEST], total: 1 } } })
    render_()

    expect(await screen.findByText(/LEM never automatically retries a failed invite/)).toBeDefined()
    const retry = screen.getByRole('button', { name: 'Retry' })

    put.mockResolvedValue({ data: { detail: 'Connection request updated' } })
    fireEvent.click(retry)

    await waitFor(() =>
      expect(put).toHaveBeenCalledWith('/connection_request', { session_token: 'tok', request_id: 9, action: 'retry' }))
  })

  it('does not offer Retry or Cancel on a non-failed request status combo it does not apply to', async () => {
    get.mockResolvedValue({ data: { detail: { requests: [PENDING_REQUEST], total: 1 } } })
    render_()

    await waitFor(() => expect(screen.getByRole('button', { name: 'Approve' })).toBeDefined())
    expect(screen.queryByRole('button', { name: 'Retry' })).toBeNull()
  })
})
