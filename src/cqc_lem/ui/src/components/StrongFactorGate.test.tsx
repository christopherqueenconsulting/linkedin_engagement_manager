import { describe, expect, it, vi, afterEach, beforeEach } from 'vitest'
import { cleanup, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import StrongFactorGate from './StrongFactorGate'
import Layout from './Layout'
import type { ReactNode } from 'react'

const auth = {
  user: { userId: 1, email: 'held@example.com' },
  sessionToken: 'cookie',
  isAdmin: false,
  enrollmentRequired: true,
  strongFactorPrompt: true,
  strongFactorDeadline: '2020-01-01T00:00:00Z',
  refreshSession: vi.fn().mockResolvedValue(undefined),
  logout: vi.fn(),
  openLoginModal: vi.fn(),
}
const get = vi.fn()

vi.mock('../contexts/useAuth', () => ({ useAuth: () => auth }))
vi.mock('../api/client', () => ({ default: { get: (...a: unknown[]) => get(...a) } }))
vi.mock('../pages/account/AuthFactorsCard', () => ({
  default: () => <div data-testid="auth-factors-card" />,
}))
vi.mock('./AccountReadinessBanner', () => ({ default: () => null }))
vi.mock('./FeedbackWidget', () => ({ default: () => null }))
vi.mock('./FloatingDock', () => ({ default: ({ children }: { children: ReactNode }) => <div>{children}</div> }))
vi.mock('./Footer', () => ({ default: () => null }))
vi.mock('./PostHogSurveyModal', () => ({ default: () => <div data-testid="ph-survey" /> }))
vi.mock('./ShippedNotice', () => ({ default: () => <div data-testid="shipped" /> }))
vi.mock('./SurveyModal', () => ({ default: () => <div data-testid="survey" /> }))
vi.mock('./StrongFactorPrompt', () => ({ default: () => <div data-testid="prompt" /> }))

beforeEach(() => {
  auth.enrollmentRequired = true
  auth.refreshSession.mockClear()
  get.mockResolvedValue({ data: { detail: { has_strong_factor: false } } })
})
afterEach(cleanup)

function harness(node: ReactNode) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>{node}</MemoryRouter>
    </QueryClientProvider>,
  )
}

// Past REQUIRE_STRONG_FACTOR_AFTER a factor-less PIN login is signed in but HELD: it reaches only
// the enrolment surface server-side (issue #905). This is that surface's screen.
describe('StrongFactorGate', () => {
  it('explains the hold and offers the enrolment UI', () => {
    harness(<StrongFactorGate />)
    expect(screen.getByTestId('strong-factor-gate')).toBeTruthy()
    expect(screen.getByTestId('auth-factors-card')).toBeTruthy()
  })

  it('is never a lockout — signing out is always available', () => {
    harness(<StrongFactorGate />)
    expect(screen.getByRole('button', { name: 'Sign out instead' })).toBeTruthy()
  })

  it('re-reads the session once a factor lands, so it can drop itself', async () => {
    get.mockResolvedValue({ data: { detail: { has_strong_factor: true } } })
    harness(<StrongFactorGate />)
    await waitFor(() => expect(auth.refreshSession).toHaveBeenCalled())
  })

  it('does not re-read the session while nothing is enrolled', async () => {
    harness(<StrongFactorGate />)
    await waitFor(() => expect(get).toHaveBeenCalled())
    expect(auth.refreshSession).not.toHaveBeenCalled()
  })
})

describe('Layout while a session is held', () => {
  it('REPLACES the app with the gate — every page behind it would 403', () => {
    harness(<Layout />)
    expect(screen.getByTestId('strong-factor-gate')).toBeTruthy()
    expect(screen.queryByTestId('shipped')).toBeNull()
    expect(screen.queryByTestId('prompt')).toBeNull()
  })

  it('stands the survey modals down — they poll outside the enrolment surface', () => {
    harness(<Layout />)
    expect(screen.queryByTestId('survey')).toBeNull()
    expect(screen.queryByTestId('ph-survey')).toBeNull()
  })

  it('renders the app normally when nothing is held', () => {
    auth.enrollmentRequired = false
    harness(<Layout />)
    expect(screen.queryByTestId('strong-factor-gate')).toBeNull()
    expect(screen.getByTestId('prompt')).toBeTruthy()
    expect(screen.getByTestId('survey')).toBeTruthy()
  })
})
