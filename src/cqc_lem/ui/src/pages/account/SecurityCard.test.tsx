import { describe, expect, it, vi, afterEach, beforeEach } from 'vitest'
import type { ReactNode } from 'react'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import SecurityCard from './SecurityCard'

const get = vi.fn()
const post = vi.fn()
vi.mock('../../api/client', () => ({
  default: {
    get: (...args: unknown[]) => get(...args),
    post: (...args: unknown[]) => post(...args),
  },
}))
vi.mock('../../contexts/useAuth', () => ({ useAuth: () => ({ sessionToken: 'cookie' }) }))

function payload(overrides: Record<string, unknown> = {}) {
  return {
    data: {
      detail: {
        public_uid: 'aa11-bb22',
        email: 'me@example.com',
        sessions: [
          { id: 1, label: 'Chrome on macOS', created_at: '2026-08-01T00:00:00Z',
            last_seen_at: '2026-08-01T01:00:00Z', expires_at: null, is_current: true, scope: 'full' },
          { id: 2, label: 'Safari on iPhone', created_at: '2026-07-30T00:00:00Z',
            last_seen_at: '2026-07-31T00:00:00Z', expires_at: null, is_current: false, scope: 'full' },
        ],
        recent_events: [
          { event: 'login_success', success: true, user_agent: null, created_at: '2026-08-01T00:00:00Z' },
          { event: 'login_failed', success: false, user_agent: null, created_at: '2026-07-31T00:00:00Z' },
        ],
        ...overrides,
      },
    },
  }
}

/** A 403 shaped exactly like the server's step-up refusal. */
function stepUpRefusal(methods: string[]) {
  return {
    response: { status: 403, data: { detail: { code: 'step_up_required', methods } } },
  }
}

function harness(ui: ReactNode) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(<QueryClientProvider client={client}>{ui}</QueryClientProvider>)
}

beforeEach(() => {
  get.mockReset()
  post.mockReset()
})
afterEach(cleanup)

describe('SecurityCard (issue #745, phase 2b)', () => {
  it('lists signed-in devices and marks the current one', async () => {
    get.mockResolvedValue(payload())
    harness(<SecurityCard />)
    await waitFor(() => expect(screen.getByText('Safari on iPhone')).toBeTruthy())
    expect(screen.getByText('This device')).toBeTruthy()
  })

  it('never renders a session token or hash', async () => {
    get.mockResolvedValue(payload())
    const { container } = harness(<SecurityCard />)
    await waitFor(() => expect(screen.getByText('Chrome on macOS')).toBeTruthy())
    expect(container.textContent).not.toContain('cookie')
  })

  it('revokes one device by id', async () => {
    get.mockResolvedValue(payload())
    post.mockResolvedValue({ data: { detail: { revoked: 1 } } })
    harness(<SecurityCard />)
    await waitFor(() => expect(screen.getAllByRole('button', { name: /Sign out$/i }).length).toBe(2))
    fireEvent.click(screen.getAllByRole('button', { name: /Sign out$/i })[1])
    await waitFor(() =>
      expect(post).toHaveBeenCalledWith('/user/sessions/revoke', {
        session_token: 'cookie',
        session_id: 2,
      })
    )
  })

  it('offers "sign out all other devices" only when another device exists', async () => {
    get.mockResolvedValue(payload({
      sessions: [{ id: 1, label: 'Chrome on macOS', created_at: null, last_seen_at: null,
                   expires_at: null, is_current: true }],
    }))
    harness(<SecurityCard />)
    await waitFor(() => expect(screen.getByText('Chrome on macOS')).toBeTruthy())
    expect(screen.queryByText(/Sign out all other devices/i)).toBeNull()
  })

  it('sends the confirmation code to the NEW address, then confirms the change', async () => {
    get.mockResolvedValue(payload())
    post.mockResolvedValue({ data: { detail: { message: 'Confirmation PIN sent' } } })
    harness(<SecurityCard />)
    await waitFor(() => expect(screen.getByLabelText(/New email address/i)).toBeTruthy())

    fireEvent.change(screen.getByLabelText(/New email address/i), {
      target: { value: 'new@example.com' },
    })
    fireEvent.click(screen.getByRole('button', { name: /Send code/i }))
    await waitFor(() =>
      expect(post).toHaveBeenCalledWith('/user/email/change/init', {
        session_token: 'cookie',
        new_email: 'new@example.com',
      })
    )

    // The PIN field only appears once a code was actually sent.
    await waitFor(() => expect(screen.getByLabelText(/Confirmation code/i)).toBeTruthy())
    fireEvent.change(screen.getByLabelText(/Confirmation code/i), { target: { value: '123456' } })
    fireEvent.click(screen.getByRole('button', { name: /Confirm change/i }))
    await waitFor(() =>
      expect(post).toHaveBeenCalledWith('/user/email/change/verify', {
        session_token: 'cookie',
        new_email: 'new@example.com',
        pin: '123456',
      })
    )
  })

  it('surfaces the API reason when an email change is rejected', async () => {
    get.mockResolvedValue(payload())
    post.mockRejectedValue({ response: { data: { detail: 'That address cannot be used' } } })
    harness(<SecurityCard />)
    await waitFor(() => expect(screen.getByLabelText(/New email address/i)).toBeTruthy())
    fireEvent.change(screen.getByLabelText(/New email address/i), {
      target: { value: 'taken@example.com' },
    })
    fireEvent.click(screen.getByRole('button', { name: /Send code/i }))
    await waitFor(() => expect(screen.getByText('That address cannot be used')).toBeTruthy())
    expect(screen.queryByLabelText(/Confirmation code/i)).toBeNull()
  })

  it('labels failed sign-ins in the recent activity list', async () => {
    get.mockResolvedValue(payload())
    harness(<SecurityCard />)
    await waitFor(() => expect(screen.getByText('Failed sign-in')).toBeTruthy())
  })

  it('badges an agent-scoped row so it is not mistaken for a stale browser login', async () => {
    get.mockResolvedValue(payload({
      sessions: [
        { id: 1, label: 'Chrome on macOS', created_at: null, last_seen_at: null,
          expires_at: null, is_current: true, scope: 'full' },
        { id: 3, label: 'Headless agent', created_at: null, last_seen_at: null,
          expires_at: null, is_current: false, scope: 'agent' },
      ],
    }))
    harness(<SecurityCard />)
    await waitFor(() => expect(screen.getByText('Headless agent')).toBeTruthy())
    expect(screen.getByText('Agent')).toBeTruthy()
    // The full-scope row never gets the badge.
    expect(screen.queryAllByText('Agent').length).toBe(1)
  })

  describe('minting an agent token (issue #1731)', () => {
    it('submits the default label and TTL, and shows the token exactly once with its expiry', async () => {
      get.mockResolvedValue(payload())
      post.mockResolvedValue({ data: { detail: { session_token: 'agent-tok-123', expires_in_days: 90 } } })
      harness(<SecurityCard />)
      await waitFor(() => expect(screen.getByRole('button', { name: /Create agent token/i })).toBeTruthy())

      fireEvent.click(screen.getByRole('button', { name: /Create agent token/i }))
      await waitFor(() =>
        expect(post).toHaveBeenCalledWith('/user/agent-token', {
          session_token: 'cookie',
          label: 'Headless agent',
          ttl_days: 90,
        })
      )

      await waitFor(() => expect(screen.getByText('agent-tok-123')).toBeTruthy())
      expect(screen.getByText(/shown only once|shown again/i)).toBeTruthy()
      expect(screen.getByText(/Expires in 90 days/i)).toBeTruthy()
      expect(screen.getByRole('button', { name: /Copy token/i })).toBeTruthy()
    })

    it('sends a custom label and TTL', async () => {
      get.mockResolvedValue(payload())
      post.mockResolvedValue({ data: { detail: { session_token: 'tok', expires_in_days: 30 } } })
      harness(<SecurityCard />)
      await waitFor(() => expect(screen.getByLabelText(/Agent token label/i)).toBeTruthy())

      fireEvent.change(screen.getByLabelText(/Agent token label/i), { target: { value: 'Backfill bot' } })
      fireEvent.change(screen.getByLabelText(/Agent token TTL in days/i), { target: { value: '30' } })
      fireEvent.click(screen.getByRole('button', { name: /Create agent token/i }))
      await waitFor(() =>
        expect(post).toHaveBeenCalledWith('/user/agent-token', {
          session_token: 'cookie',
          label: 'Backfill bot',
          ttl_days: 30,
        })
      )
    })

    it('opens the step-up modal on a 403 and re-mints once verified', async () => {
      get.mockResolvedValue(payload())
      post
        .mockRejectedValueOnce(stepUpRefusal(['totp']))
        .mockResolvedValueOnce({ data: { detail: { verified: true } } })
        .mockResolvedValueOnce({ data: { detail: { session_token: 'tok-after-stepup', expires_in_days: 90 } } })
      harness(<SecurityCard />)
      await waitFor(() => expect(screen.getByRole('button', { name: /Create agent token/i })).toBeTruthy())

      fireEvent.click(screen.getByRole('button', { name: /Create agent token/i }))
      await waitFor(() => expect(screen.getByText("Confirm it's you")).toBeTruthy())

      fireEvent.change(screen.getByLabelText('Authenticator code'), { target: { value: '123456' } })
      fireEvent.click(screen.getByText('Confirm'))

      await waitFor(() =>
        expect(post.mock.calls.filter((c) => c[0] === '/user/agent-token').length).toBe(2))
      await waitFor(() => expect(screen.getByText('tok-after-stepup')).toBeTruthy())
    })

    it('shows an error and no token when the mint fails for a reason other than step-up', async () => {
      get.mockResolvedValue(payload())
      post.mockRejectedValue({ response: { status: 500, data: { detail: 'boom' } } })
      harness(<SecurityCard />)
      await waitFor(() => expect(screen.getByRole('button', { name: /Create agent token/i })).toBeTruthy())

      fireEvent.click(screen.getByRole('button', { name: /Create agent token/i }))
      await waitFor(() =>
        expect(screen.getByText(/Could not create an agent token/i)).toBeTruthy())
      expect(screen.queryByRole('button', { name: /Copy token/i })).toBeNull()
    })
  })
})
