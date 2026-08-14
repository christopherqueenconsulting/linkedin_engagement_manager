import { describe, expect, it, vi, afterEach, beforeEach } from 'vitest'
import type { ReactNode } from 'react'
import { cleanup, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import Dashboard from './Dashboard'

// Issue #1004: "Your Best Times to Post" printed `avg_engagement` raw, so an impression-normalized
// rate reached the user as a bare decimal. The formatter itself is covered in
// components/charts/palette.test.ts; what is asserted HERE is the wiring — that the card reads the
// payload's `metric` to pick the scale, so a rate becomes a percentage and a count never does.
const get = vi.fn()

vi.mock('../api/client', () => ({
  default: {
    get: (...args: unknown[]) => get(...args),
    post: vi.fn(),
    delete: vi.fn(),
  },
}))

vi.mock('../contexts/useAuth', () => ({
  useAuth: () => ({ user: { email: 'test@example.com', userId: 1 }, sessionToken: 'tok' }),
}))

vi.mock('../hooks/useUserTimezone', () => ({
  useUserTimezone: () => 'America/New_York',
  useUserTimezoneState: () => ({ timezone: 'America/New_York', isResolved: true }),
}))

vi.mock('../components/OnboardingChecklist', () => ({ default: () => null }))
vi.mock('../components/PostHogStatsPanel', () => ({ default: () => null }))

function payload(data: unknown) {
  return { data: { detail: data } }
}

function mockApi(postStats: unknown) {
  get.mockImplementation((path: string) => {
    if (path.startsWith('/user/post-stats')) return Promise.resolve(payload(postStats))
    if (path.startsWith('/activity/')) return Promise.resolve(payload([]))
    if (path.startsWith('/dashboard/planned-tasks/')) return Promise.resolve(payload({ tasks: [] }))
    return Promise.resolve(payload({}))
  })
}

function harness(ui: ReactNode) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <MemoryRouter>
      <QueryClientProvider client={client}>{ui}</QueryClientProvider>
    </MemoryRouter>,
  )
}

beforeEach(() => {
  get.mockReset()
})

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('Best Times to Post — avg engagement is a percentage only when it is a rate (issue #1004)', () => {
  it('renders an impression-normalized rate as a percentage, not a raw decimal', async () => {
    mockApi({
      recommendations: [
        { weekday: 'Wednesday', hour: 16, avg_engagement: 0.0612, sample: 4, metric: 'engagement_rate' },
      ],
      rankings: {},
      sample_size: 7,
      timezone: 'America/New_York',
    })
    harness(<Dashboard />)

    await waitFor(() =>
      expect(screen.getByText(/avg engagement rate 6\.12% · 4 post\(s\)/)).toBeDefined(),
    )
    expect(screen.queryByText(/0\.0612/)).toBeNull()
  })

  it('leaves a per-post count as a count — a count is not a percentage', async () => {
    mockApi({
      recommendations: [
        { weekday: 'Monday', hour: 9, avg_engagement: 12.5, sample: 3, metric: 'engagement' },
      ],
      rankings: {},
      sample_size: 5,
      timezone: 'America/New_York',
    })
    harness(<Dashboard />)

    const row = await waitFor(() => screen.getByText(/avg engagement 12\.5 · 3 post\(s\)/))
    expect(row.textContent).not.toContain('%')
    expect(screen.queryByText(/avg engagement rate/)).toBeNull()
  })

  it('reads a payload with no metric as a count rather than inventing a percentage', async () => {
    mockApi({
      recommendations: [{ weekday: 'Friday', hour: 11, avg_engagement: 9, sample: 3 }],
      rankings: {},
      sample_size: 4,
      timezone: 'America/New_York',
    })
    harness(<Dashboard />)

    await waitFor(() => expect(screen.getByText(/avg engagement 9 · 3 post\(s\)/)).toBeDefined())
    expect(screen.queryByText(/900\.00%/)).toBeNull()
  })
})
