import { describe, expect, it, vi, afterEach, beforeEach } from 'vitest'
import type { ReactNode } from 'react'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import AuthFactorsCard from './AuthFactorsCard'

const get = vi.fn()
const post = vi.fn()
vi.mock('../../api/client', () => ({
  default: {
    get: (...args: unknown[]) => get(...args),
    post: (...args: unknown[]) => post(...args),
  },
}))
vi.mock('../../contexts/AuthContext', () => ({ useAuth: () => ({ sessionToken: 'cookie' }) }))
vi.mock('../../utils/webauthn', () => ({
  isPasskeySupported: () => true,
  createPasskey: vi.fn(async () => ({ id: 'cred' })),
  getPasskeyAssertion: vi.fn(async () => ({ id: 'cred' })),
}))

function payload(overrides: Record<string, unknown> = {}) {
  return {
    data: {
      detail: {
        factors: [],
        recovery_codes_unused: 0,
        recovery_codes_total: 0,
        passkeys_supported: true,
        has_strong_factor: false,
        pin_is_bootstrap_only: false,
        step_up_satisfied: true,
        ...overrides,
      },
    },
  }
}

function harness(ui: ReactNode) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(<QueryClientProvider client={client}>{ui}</QueryClientProvider>)
}

/** A 403 shaped exactly like the server's step-up refusal. */
function stepUpRefusal(methods: string[]) {
  return {
    response: { status: 403, data: { detail: { code: 'step_up_required', methods } } },
  }
}

beforeEach(() => {
  get.mockReset()
  post.mockReset()
})
afterEach(cleanup)

describe('AuthFactorsCard (issue #745, phase 2c)', () => {
  it('tells an unprotected account what is actually at stake', async () => {
    get.mockResolvedValue(payload())
    harness(<AuthFactorsCard />)
    await waitFor(() =>
      expect(screen.getByText(/anyone who can read your email can sign in as you/i)).toBeTruthy())
  })

  it('says the emailed code has been demoted once a factor exists', async () => {
    get.mockResolvedValue(payload({
      factors: [{ id: 1, kind: 'passkey', label: 'iPhone', created_at: null, last_used_at: null }],
      has_strong_factor: true,
      pin_is_bootstrap_only: true,
    }))
    harness(<AuthFactorsCard />)
    await waitFor(() =>
      expect(screen.getByText(/no longer sign you in/i)).toBeTruthy())
    expect(screen.getByText(/Passkey/)).toBeTruthy()
  })

  it('hides the passkey button when the deployment cannot serve them', async () => {
    get.mockResolvedValue(payload({ passkeys_supported: false }))
    harness(<AuthFactorsCard />)
    await waitFor(() => expect(screen.getByText('Use an authenticator app')).toBeTruthy())
    expect(screen.queryByText('Add a passkey')).toBeNull()
  })

  it('shows the TOTP secret to scan and confirms it with a code', async () => {
    get.mockResolvedValue(payload())
    post.mockResolvedValueOnce({ data: { detail: { secret: 'JBSWY3DP', otpauth_uri: 'otpauth://x' } } })
    harness(<AuthFactorsCard />)
    await waitFor(() => expect(screen.getByText('Use an authenticator app')).toBeTruthy())

    fireEvent.click(screen.getByText('Use an authenticator app'))
    await waitFor(() => expect(screen.getByTestId('totp-secret').textContent).toBe('JBSWY3DP'))

    post.mockResolvedValueOnce({ data: { detail: {} } })
    fireEvent.change(screen.getByLabelText('Authenticator code'), { target: { value: '123456' } })
    fireEvent.click(screen.getByText('Confirm'))
    await waitFor(() =>
      expect(post.mock.calls.some((c) => c[0] === '/user/totp/enroll/confirm')).toBe(true))
  })

  it('shows fresh recovery codes once, with the warning that they will not be shown again', async () => {
    get.mockResolvedValue(payload())
    post.mockResolvedValueOnce({ data: { detail: { codes: ['AAAA111111', 'BBBB222222'] } } })
    harness(<AuthFactorsCard />)
    await waitFor(() => expect(screen.getByText('Generate recovery codes')).toBeTruthy())

    fireEvent.click(screen.getByText('Generate recovery codes'))
    await waitFor(() => expect(screen.getByTestId('recovery-codes')).toBeTruthy())
    expect(screen.getByText('AAAA111111')).toBeTruthy()
    expect(screen.getByText(/only time they will be shown/i)).toBeTruthy()
  })

  it('asks for a factor when the server refuses, then retries the same write', async () => {
    // This is the whole point of the guard: the user sees "confirm it's you", not an error.
    get.mockResolvedValue(payload({
      factors: [{ id: 4, kind: 'totp', label: 'Authenticator app', created_at: null,
                  last_used_at: null }],
      has_strong_factor: true,
    }))
    post
      .mockRejectedValueOnce(stepUpRefusal(['totp']))            // the delete, refused
      .mockResolvedValueOnce({ data: { detail: { verified: true } } })  // step-up verify
      .mockResolvedValueOnce({ data: { detail: { removed: 1 } } })      // the delete, retried
    harness(<AuthFactorsCard />)
    await waitFor(() => expect(screen.getByText('Remove')).toBeTruthy())

    fireEvent.click(screen.getByText('Remove'))
    await waitFor(() => expect(screen.getByText("Confirm it's you")).toBeTruthy())

    fireEvent.change(screen.getByLabelText('Authenticator code'), { target: { value: '123456' } })
    fireEvent.click(screen.getByText('Confirm'))

    await waitFor(() =>
      expect(post.mock.calls.filter((c) => c[0] === '/user/auth-factors/delete').length).toBe(2))
    expect(post.mock.calls.some((c) => c[0] === '/user/step-up/verify')).toBe(true)
  })

  it('cancelling the step-up leaves the write undone', async () => {
    get.mockResolvedValue(payload({
      factors: [{ id: 4, kind: 'totp', label: 'Authenticator app', created_at: null,
                  last_used_at: null }],
      has_strong_factor: true,
    }))
    post.mockRejectedValueOnce(stepUpRefusal(['totp']))
    harness(<AuthFactorsCard />)
    await waitFor(() => expect(screen.getByText('Remove')).toBeTruthy())

    fireEvent.click(screen.getByText('Remove'))
    await waitFor(() => expect(screen.getByText("Confirm it's you")).toBeTruthy())
    fireEvent.click(screen.getByText('Cancel'))

    await waitFor(() => expect(screen.queryByText("Confirm it's you")).toBeNull())
    expect(post.mock.calls.filter((c) => c[0] === '/user/auth-factors/delete').length).toBe(1)
  })
})
