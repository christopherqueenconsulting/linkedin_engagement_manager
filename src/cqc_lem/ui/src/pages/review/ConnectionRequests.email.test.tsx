import { describe, expect, it, vi, afterEach, beforeEach } from 'vitest'
import type { ReactNode } from 'react'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import ConnectionRequests from './ConnectionRequests'

// Issue #1836 — a few profiles render an email-verification variant of LinkedIn's Connect dialog and
// refuse the invite without the recipient's address. This is the ONLY way one enters LEM: a human
// types it against one specific row. Nothing derives it from contact data already in the database,
// so these tests assert the typed value is what gets sent, and that the address never comes back.
const get = vi.fn()
const post = vi.fn()
const put = vi.fn()

vi.mock('../../api/client', () => ({
  default: {
    get: (...args: unknown[]) => get(...args),
    post: (...args: unknown[]) => post(...args),
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
  created_at: '2026-09-01T13:00:00+00:00',
  source: null,
  icp_score: null,
  reasons: null,
  failure_reason: "Connect dialog requires the recipient's email to verify the connection, which we do not have",
  has_recipient_email: false,
}

function harness(ui: ReactNode) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  return render(<QueryClientProvider client={client}>{ui}</QueryClientProvider>)
}

beforeEach(() => {
  get.mockReset()
  post.mockReset()
  put.mockReset()
})

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

const render_ = () => harness(<ConnectionRequests userTimezone="UTC" />)

describe('ConnectionRequests recipient email (issue #1836)', () => {
  it('attaches a typed email to one row with a field-only PUT — no action, so it approves nothing', async () => {
    get.mockResolvedValue({ data: { detail: { requests: [FAILED_REQUEST], total: 1 } } })
    render_()

    fireEvent.click(await screen.findByRole('button', { name: 'Add email' }))
    fireEvent.change(screen.getByLabelText('Recipient email for Jane Doe'),
      { target: { value: 'jane@example.com' } })

    put.mockResolvedValue({ data: { detail: 'Connection request updated' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save email' }))

    await waitFor(() => expect(put).toHaveBeenCalledWith('/connection_request', {
      session_token: 'tok', request_id: 9, recipient_email: 'jane@example.com',
    }))
    // The guarantee that matters: a field-only save carries no `action`, which is the one field the
    // server gates approval on. Saving an address can never become approving a send.
    expect(put.mock.calls[0][1]).not.toHaveProperty('action')
  })

  it('reports a stored address as a boolean and never renders the address itself', async () => {
    get.mockResolvedValue({
      data: { detail: { requests: [{ ...FAILED_REQUEST, has_recipient_email: true }], total: 1 } },
    })
    render_()

    expect(await screen.findByText('Email on file')).toBeDefined()
    expect(screen.getByRole('button', { name: 'Replace email' })).toBeDefined()
    expect(screen.queryByRole('button', { name: 'Add email' })).toBeNull()
  })

  it('sends null rather than an empty string when a new target is added with no email', async () => {
    get.mockResolvedValue({ data: { detail: { requests: [], total: 0 } } })
    render_()

    await screen.findByText('No connection requests yet.')
    fireEvent.change(screen.getByPlaceholderText(/Profile URL/),
      { target: { value: 'https://www.linkedin.com/in/jane' } })

    post.mockResolvedValue({ data: { detail: { request_id: 1 } } })
    fireEvent.click(screen.getByRole('button', { name: 'Add target' }))

    await waitFor(() => expect(post).toHaveBeenCalled())
    expect(post.mock.calls[0][1]).toMatchObject({ recipient_email: null })
  })

  it('offers no email control on a terminal row, whose stored address LEM has already cleared', async () => {
    get.mockResolvedValue({
      data: { detail: { requests: [{ ...FAILED_REQUEST, id: 11, status: 'sent', failure_reason: null }], total: 1 } },
    })
    render_()

    await waitFor(() => expect(screen.getByText('SENT')).toBeDefined())
    expect(screen.queryByRole('button', { name: 'Add email' })).toBeNull()
  })
})
