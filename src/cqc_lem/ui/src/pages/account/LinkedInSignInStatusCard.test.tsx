import { describe, expect, it, vi, afterEach, beforeEach } from 'vitest'
import type { ReactNode } from 'react'
import { cleanup, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import LinkedInSignInStatusCard from './LinkedInSignInStatusCard'

const get = vi.fn()
vi.mock('../../api/client', () => ({
  default: { get: (...args: unknown[]) => get(...args) },
}))
vi.mock('../../contexts/useAuth', () => ({ useAuth: () => ({ sessionToken: 'tok' }) }))

function payload(detail: Record<string, unknown>) {
  return {
    data: {
      detail: {
        signed_in_at: null,
        approval_requested_at: null,
        approval_cleared_at: null,
        ...detail,
      },
    },
  }
}

function harness(ui: ReactNode) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(<QueryClientProvider client={client}>{ui}</QueryClientProvider>)
}

// Braces matter: a value returned from beforeEach is a teardown callback to vitest, and
// mockReset() returns the mock itself — so vitest would call get() after every test.
beforeEach(() => {
  get.mockReset()
})
afterEach(cleanup)

describe('LinkedInSignInStatusCard (issue #933)', () => {
  it('confirms a sign-in that followed the approval the user already made', async () => {
    get.mockResolvedValue(
      payload({
        state: 'signed_in',
        signed_in_at: '2026-08-01T12:00:00+00:00',
        approval_cleared_at: '2026-08-01T12:00:00+00:00',
      }),
    )
    harness(<LinkedInSignInStatusCard />)
    await waitFor(() => expect(screen.getByText(/LinkedIn sign-in confirmed/i)).toBeTruthy())
    // The exact fact the reporter could not see anywhere in the product.
    expect(screen.getByText(/device approval came through/i)).toBeTruthy()
  })

  it('reports a plain sign-in without claiming an approval happened', async () => {
    get.mockResolvedValue(
      payload({ state: 'signed_in', signed_in_at: '2026-08-01T12:00:00+00:00' }),
    )
    harness(<LinkedInSignInStatusCard />)
    await waitFor(() => expect(screen.getByText(/LinkedIn sign-in confirmed/i)).toBeTruthy())
    expect(screen.queryByText(/device approval came through/i)).toBeNull()
  })

  it('tells a waiting user what to tap and that it clears itself', async () => {
    get.mockResolvedValue(
      payload({ state: 'approval_pending', approval_requested_at: '2026-08-01T12:00:00+00:00' }),
    )
    harness(<LinkedInSignInStatusCard />)
    await waitFor(() => expect(screen.getByText(/Waiting for you to approve/i)).toBeTruthy())
    expect(screen.getByText(/Already approved\?/i)).toBeTruthy()
  })

  it('says a missed approval will simply be asked again', async () => {
    get.mockResolvedValue(
      payload({
        state: 'approval_timed_out',
        approval_requested_at: '2026-08-01T12:00:00+00:00',
        signed_in_at: '2026-07-30T09:00:00+00:00',
      }),
    )
    harness(<LinkedInSignInStatusCard />)
    await waitFor(() => expect(screen.getByText(/not approved in time/i)).toBeTruthy())
    expect(screen.getByText(/Last successful sign-in/i)).toBeTruthy()
  })

  it('flags an unsolvable challenge instead of reading as "nothing recorded" (issue #1920)', async () => {
    get.mockResolvedValue(
      payload({
        state: 'challenge_unsolvable',
        signed_in_at: '2026-08-01T12:00:00+00:00',
      }),
    )
    harness(<LinkedInSignInStatusCard />)
    await waitFor(() =>
      expect(screen.getByText(/verification automation could not clear/i)).toBeTruthy(),
    )
    expect(screen.queryByText(/No LinkedIn sign-in recorded yet/i)).toBeNull()
    expect(screen.getByText(/Last successful sign-in/i)).toBeTruthy()
  })

  it('reads an empty record as "nothing yet", never as a broken connection', async () => {
    get.mockResolvedValue(payload({ state: 'unknown' }))
    harness(<LinkedInSignInStatusCard />)
    await waitFor(() => expect(screen.getByText(/No LinkedIn sign-in recorded yet/i)).toBeTruthy())
    expect(screen.getByText(/Nothing is wrong/i)).toBeTruthy()
  })

  it('renders nothing until the status arrives', () => {
    get.mockReturnValue(new Promise(() => {}))
    harness(<LinkedInSignInStatusCard />)
    expect(screen.queryByTestId('linkedin-signin-status')).toBeNull()
  })
})
