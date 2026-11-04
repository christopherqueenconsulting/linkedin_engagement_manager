import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import LinkedInLoginCard from './LinkedInLoginCard'

const mocks = vi.hoisted(() => ({ get: vi.fn() }))
vi.mock('../../api/client', () => ({
  default: { get: (...args: unknown[]) => mocks.get(...args), put: vi.fn(), post: vi.fn() },
}))
vi.mock('../../contexts/useAuth', () => ({ useAuth: () => ({ sessionToken: 'tok' }) }))
vi.mock('../../hooks/useAccountReadiness', () => ({
  useAccountReadiness: () => ({ data: { items: [], cookie_migration_needed: false } }),
}))
vi.mock('../../hooks/useStepUp', () => ({
  useStepUp: () => ({ guard: (fn: () => unknown) => fn(), stepUpModal: null }),
}))
vi.mock('../../components/LinkedInSessionCard', () => ({
  default: () => <div data-testid="session-card" />,
}))
vi.mock('./LinkedInSignInStatusCard', () => ({
  default: () => <div data-testid="signin-status-card" />,
}))
vi.mock('./MemberPostAnalyticsConnectionCard', () => ({
  default: () => <div data-testid="analytics-card" />,
}))
function serve(mode: 'suggest' | 'automate' | 'error', healthyToken = true) {
  mocks.get.mockImplementation((url: string) => {
    if (url.startsWith('/user/settings')) {
      if (mode === 'error') return Promise.reject(new Error('503'))
      return Promise.resolve({ data: { detail: { engagement_mode: mode } } })
    }
    if (url.startsWith('/user/token_status')) {
      if (!healthyToken) return new Promise(() => {})
      return Promise.resolve({
        data: {
          detail: {
            token_expiry_date: null,
            days_remaining: 40,
            is_expiring_soon: false,
            is_expired: false,
            can_auto_refresh: true,
            refresh_attempted: false,
            refresh_succeeded: false,
          },
        },
      })
    }
    return Promise.reject(new Error(`unexpected GET ${url}`))
  })
}

function harness() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <LinkedInLoginCard />
    </QueryClientProvider>,
  )
}

beforeEach(() => {
  mocks.get.mockReset()
  localStorage.clear()
})
afterEach(cleanup)

describe('LinkedInLoginCard engagement mode', () => {
  it('words suggest-mode connection as approved posts plus suggestions', () => {
    serve('suggest', false)
    harness()
    const text = screen.getByTestId('linkedin-connect-purpose').textContent ?? ''
    expect(text).toMatch(/publish the posts you approve/i)
    expect(text).toMatch(/suggestion mode/i)
    expect(text).not.toMatch(/on your behalf|send DMs|automatically/i)
  })

  it('hides browser credentials for suggest mode while keeping analytics connection', async () => {
    serve('suggest')
    harness()
    await waitFor(() => expect(screen.getByTestId('oauth-only-note')).toBeTruthy())
    expect(screen.getByTestId('oauth-only-note').textContent).toMatch(/LinkedIn sign-in only/)
    expect(screen.queryByTestId('session-card')).toBeNull()
    expect(screen.queryByTestId('signin-status-card')).toBeNull()
    expect(screen.queryByText(/Use a LinkedIn password instead/)).toBeNull()
    expect(screen.queryByPlaceholderText(/LinkedIn password/i)).toBeNull()
    expect(screen.getByTestId('analytics-card')).toBeTruthy()
  })

  it('keeps OAuth reconnect link for suggest mode', async () => {
    serve('suggest')
    harness()
    await waitFor(() => expect(screen.getByTestId('oauth-only-note')).toBeTruthy())
    expect(screen.getByRole('link', { name: /Reconnect/ }).getAttribute('href'))
      .toMatch(/^\/api\/auth\/linkedin\//)
  })

  it('keeps credential controls and automation wording for automate mode', async () => {
    serve('automate')
    harness()
    await waitFor(() => expect(screen.getByTestId('session-card')).toBeTruthy())
    expect(screen.getByTestId('signin-status-card')).toBeTruthy()
    expect(screen.getByText(/Use a LinkedIn password instead/)).toBeTruthy()
    expect(screen.queryByTestId('oauth-only-note')).toBeNull()
  })

  it('fails closed while engagement mode is unreadable', async () => {
    serve('error')
    harness()
    await waitFor(() => expect(screen.getByTestId('oauth-only-note')).toBeTruthy())
    expect(screen.queryByTestId('session-card')).toBeNull()
    expect(screen.queryByTestId('signin-status-card')).toBeNull()
    expect(screen.queryByText(/Use a LinkedIn password instead/)).toBeNull()
  })

  it('keeps full-automation wording for an automate account', () => {
    serve('automate', false)
    harness()
    expect(screen.getByTestId('linkedin-connect-purpose').textContent)
      .toMatch(/post on your behalf, send DMs/)
  })
})
