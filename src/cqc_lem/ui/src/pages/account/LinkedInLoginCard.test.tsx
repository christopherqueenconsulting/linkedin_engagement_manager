import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { ReactNode } from 'react'
import { cleanup, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import LinkedInLoginCard from './LinkedInLoginCard'

const get = vi.fn()
vi.mock('../../api/client', () => ({
  default: { get: (...args: unknown[]) => get(...args), put: vi.fn(), post: vi.fn() },
}))
vi.mock('../../contexts/useAuth', () => ({ useAuth: () => ({ sessionToken: 'tok' }) }))
vi.mock('../../hooks/useStepUp', () => ({
  useStepUp: () => ({ guard: (fn: () => unknown) => fn(), stepUpModal: null }),
}))
vi.mock('../../components/LinkedInSessionCard', () => ({
  default: () => <div data-testid="session-card" />,
}))
vi.mock('./LinkedInSignInStatusCard', () => ({
  default: () => <div data-testid="signin-status-card" />,
}))

type Mode = 'suggest' | 'automate'

// A connected account with a healthy token, so the deprecated password disclosure WOULD render
// for an automate account — the suggest case has to hide it, not merely never reach it.
function serve(mode: Mode | 'error') {
  get.mockImplementation((url: string) => {
    if (url.startsWith('/user/settings')) {
      return mode === 'error'
        ? Promise.reject(new Error('503'))
        : Promise.resolve({ data: { detail: { engagement_mode: mode } } })
    }
    if (url.startsWith('/user/token_status')) {
      return Promise.resolve({
        data: {
          detail: {
            token_expiry_date: null, days_remaining: 40, is_expiring_soon: false,
            is_expired: false, can_auto_refresh: true, refresh_attempted: false,
            refresh_succeeded: false,
          },
        },
      })
    }
    if (url.startsWith('/user/account-readiness')) {
      return Promise.resolve({ data: { detail: { ready: true, items: [] } } })
    }
    return Promise.reject(new Error(`unexpected GET ${url}`))
  })
}

function harness(ui: ReactNode) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(<QueryClientProvider client={client}>{ui}</QueryClientProvider>)
}

beforeEach(() => {
  get.mockReset()
  localStorage.clear()
})
afterEach(cleanup)

describe('LinkedInLoginCard engagement mode (issue #2368)', () => {
  it('hides the cookie, sign-in status and password controls for a suggest account', async () => {
    serve('suggest')
    harness(<LinkedInLoginCard />)
    await waitFor(() => expect(screen.getByTestId('oauth-only-note')).toBeTruthy())
    expect(screen.getByTestId('oauth-only-note').textContent).toMatch(/LinkedIn sign-in only/)
    expect(screen.queryByTestId('session-card')).toBeNull()
    expect(screen.queryByTestId('signin-status-card')).toBeNull()
    expect(screen.queryByText(/Use a LinkedIn password instead/)).toBeNull()
    expect(screen.queryByPlaceholderText(/LinkedIn password/i)).toBeNull()
  })

  it('keeps the LinkedIn sign-in (OAuth) reconnect link for a suggest account', async () => {
    serve('suggest')
    harness(<LinkedInLoginCard />)
    await waitFor(() => expect(screen.getByTestId('oauth-only-note')).toBeTruthy())
    const link = screen.getByRole('link', { name: /Reconnect/ })
    expect(link.getAttribute('href')).toMatch(/^\/api\/auth\/linkedin\//)
  })

  it('shows every credential control for an automate account, with no OAuth-only note', async () => {
    serve('automate')
    harness(<LinkedInLoginCard />)
    await waitFor(() => expect(screen.getByTestId('session-card')).toBeTruthy())
    expect(screen.getByTestId('signin-status-card')).toBeTruthy()
    expect(screen.getByText(/Use a LinkedIn password instead/)).toBeTruthy()
    expect(screen.queryByTestId('oauth-only-note')).toBeNull()
  })

  it('fails closed: an unreadable mode shows no credential control', async () => {
    serve('error')
    harness(<LinkedInLoginCard />)
    await waitFor(() =>
      expect(get.mock.calls.some(([url]) => String(url).startsWith('/user/settings'))).toBe(true),
    )
    await waitFor(() => expect(screen.getByRole('link', { name: /Reconnect/ })).toBeTruthy())
    expect(screen.queryByTestId('session-card')).toBeNull()
    expect(screen.queryByTestId('signin-status-card')).toBeNull()
    expect(screen.queryByText(/Use a LinkedIn password instead/)).toBeNull()
  })
})
