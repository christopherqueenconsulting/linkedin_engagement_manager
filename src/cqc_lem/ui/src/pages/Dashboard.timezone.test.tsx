import { describe, expect, it, vi, afterEach, beforeEach } from 'vitest'
import type { ReactNode } from 'react'
import { cleanup, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import Dashboard from './Dashboard'

// Issue #1003: "Your Best Times to Post" rendered a bare 24-hour clock face with no zone, so a
// recommendation read as neither the user's clock nor anyone else's. The helper arithmetic is
// covered in utils/datetime.test.ts; what is asserted HERE is the wiring the helpers hang off —
// that the list renders 12-hour, and that it is labelled with the zone the SERVER bucketed the
// hours into rather than the browser's guess.
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

// The browser/configured-zone hook, deliberately DIFFERENT from the server zone below so the two
// cannot pass for each other.
vi.mock('../hooks/useUserTimezone', () => ({
  useUserTimezone: () => 'Asia/Tokyo',
  useUserTimezoneState: () => ({ timezone: 'Asia/Tokyo', isResolved: true }),
}))

vi.mock('../components/OnboardingChecklist', () => ({ default: () => null }))
vi.mock('../components/PostHogStatsPanel', () => ({ default: () => null }))

const RECOMMENDATIONS = [
  { weekday: 'Wednesday', hour: 16, avg_engagement: 12, sample: 4 },
  { weekday: 'Monday', hour: 0, avg_engagement: 9, sample: 3 },
]

function payload(data: unknown) {
  return { data: { detail: data } }
}

// `postStats` is the only response this suite cares about; every other query on the page just has
// to resolve to something shaped like an empty payload.
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

describe('Best Times to Post — 12-hour clock and a zone label (issue #1003)', () => {
  it('renders each recommended hour 12-hour, never a 24-hour clock face', async () => {
    mockApi({ recommendations: RECOMMENDATIONS, rankings: {}, sample_size: 7, timezone: 'America/New_York' })
    harness(<Dashboard />)

    await waitFor(() => expect(screen.getByText('Wednesday @ 4:00 PM')).toBeDefined())
    expect(screen.getByText('Monday @ 12:00 AM')).toBeDefined()
    expect(screen.queryByText(/@ 16:00/)).toBeNull()
    expect(screen.queryByText(/@ 00:00/)).toBeNull()
  })

  it('labels the list with the zone the SERVER bucketed the hours into, not the browser guess', async () => {
    mockApi({ recommendations: RECOMMENDATIONS, rankings: {}, sample_size: 7, timezone: 'America/New_York' })
    harness(<Dashboard />)

    // EST or EDT depending on when the suite runs — either proves the abbreviation followed the
    // server zone, and neither is Tokyo's.
    const label = await waitFor(() => screen.getByText(/Times are in America\/New_York \(E[SD]T\)/))
    expect(label).toBeDefined()
    expect(screen.queryByText(/Asia\/Tokyo/)).toBeNull()
  })

  it('falls back to the user zone only when the payload carries no timezone', async () => {
    mockApi({ recommendations: RECOMMENDATIONS, rankings: {}, sample_size: 7 })
    harness(<Dashboard />)

    await waitFor(() => expect(screen.getByText(/Times are in Asia\/Tokyo \(GMT\+9\)/)).toBeDefined())
  })

  it('says nothing about a zone when there is nothing to recommend yet', async () => {
    mockApi({ recommendations: [], rankings: {}, sample_size: 1 })
    harness(<Dashboard />)

    await waitFor(() => expect(screen.getByText(/recommendations appear after a few posts/)).toBeDefined())
    expect(screen.queryByText(/Times are in/)).toBeNull()
  })
})
