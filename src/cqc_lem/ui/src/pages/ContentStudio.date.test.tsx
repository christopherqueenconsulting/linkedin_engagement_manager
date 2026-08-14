import { describe, expect, it, vi, afterEach, beforeEach } from 'vitest'
import type { ReactNode } from 'react'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import ContentStudio from './ContentStudio'
import { getTodayInTz, resolveDateRange } from '../utils/dateRange'

const get = vi.fn()
const post = vi.fn()

vi.mock('../api/client', () => ({
  default: {
    get: (...args: unknown[]) => get(...args),
    post: (...args: unknown[]) => post(...args),
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

vi.mock('../utils/analytics', () => ({
  capture: vi.fn(),
  recordPostApproval: vi.fn(),
  maskProps: (className: string) => ({ className }),
  EVENTS: { postApproved: 'post_approved', postRejected: 'post_rejected' },
}))

vi.mock('./content/ComposePost', () => ({
  default: () => null,
}))
vi.mock('./review/NewsletterQueue', () => ({
  default: () => null,
}))
vi.mock('./review/ScheduledDMs', () => ({
  default: () => null,
}))
vi.mock('./review/ConnectionRequests', () => ({
  default: () => null,
}))
vi.mock('./review/OutreachFunnel', () => ({
  default: () => null,
}))
vi.mock('./review/LeadsInbox', () => ({
  default: () => null,
}))
vi.mock('./review/LeadsPipeline', () => ({
  default: () => null,
}))
vi.mock('./review/CatchupTouches', () => ({
  default: () => null,
}))
vi.mock('./review/GroupPostQueue', () => ({
  default: () => null,
}))

const POST_BASE = {
  post_id: 1,
  content: 'Draft content',
  video_url: null,
  scheduled_time: '2026-07-30T14:00:00Z',
  carousel_slides: null,
  post_url: null,
  archetype: null,
  authenticity_score: null,
  gate_reason: null,
  rejection_reason: null,
}

function payload(data: unknown) {
  return { data: { detail: data } }
}

function harness(ui: ReactNode) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <MemoryRouter initialEntries={['/?tab=review']}>
      <QueryClientProvider client={client}>{ui}</QueryClientProvider>
    </MemoryRouter>
  )
}

beforeEach(() => {
  get.mockReset()
  post.mockReset()
})

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

function mockPosts(posts: unknown[]) {
  get.mockImplementation((path: string) => {
    if (path.startsWith('/posts/')) return Promise.resolve(payload({ posts, total: posts.length, page: 1, page_size: 10 }))
    if (path.startsWith('/user/timezone')) return Promise.resolve(payload({ timezone: 'America/New_York' }))
    if (path.startsWith('/content_generation_status')) return Promise.resolve(payload(null))
    return Promise.resolve(payload({}))
  })
}

describe('ContentStudio date range helpers', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    // Pin system time to 2026-07-29 12:00 UTC -> 08:00 EDT
    vi.setSystemTime(new Date('2026-07-29T12:00:00Z'))
  })

  afterEach(() => {
    vi.useRealTimers()
  })

  it('getTodayInTz returns the wall date in the user timezone', () => {
    expect(getTodayInTz('America/New_York')).toBe('2026-07-29')
    expect(getTodayInTz('UTC')).toBe('2026-07-29')
    // A timezone well ahead of UTC should already be the next day at this UTC instant.
    expect(getTodayInTz('Asia/Tokyo')).toBe('2026-07-29')
  })

  it('resolveDateRange handles preset ranges relative to user today', () => {
    expect(resolveDateRange('today', 'America/New_York')).toEqual({
      start: '2026-07-29',
      end: '2026-07-29',
    })
    expect(resolveDateRange('yesterday', 'America/New_York')).toEqual({
      start: '2026-07-28',
      end: '2026-07-28',
    })
    expect(resolveDateRange('last7days', 'America/New_York')).toEqual({
      start: '2026-07-23',
      end: '2026-07-29',
    })
    expect(resolveDateRange('last30days', 'America/New_York')).toEqual({
      start: '2026-06-30',
      end: '2026-07-29',
    })
    expect(resolveDateRange('thisMonth', 'America/New_York')).toEqual({
      start: '2026-07-01',
      end: '2026-07-31',
    })
    expect(resolveDateRange('next30days', 'America/New_York')).toEqual({
      start: '2026-07-29',
      end: '2026-08-27',
    })
  })

  it('resolveDateRange custom returns provided dates', () => {
    expect(resolveDateRange('custom', 'America/New_York', '2026-01-01', '2026-01-31')).toEqual({
      start: '2026-01-01',
      end: '2026-01-31',
    })
  })

  it('resolveDateRange ALL returns null bounds', () => {
    expect(resolveDateRange('ALL', 'America/New_York')).toEqual({ start: null, end: null })
  })
})

describe('ContentStudio date range filter', () => {
  it('sends start_date and end_date when the Today preset is selected', async () => {
    mockPosts([{ ...POST_BASE, post_type: 'text', status: 'pending' }])
    harness(<ContentStudio />)

    await waitFor(() => expect(screen.getByText('Draft content')).toBeDefined())

    const dateSelect = screen.getByLabelText('Date range preset')
    fireEvent.change(dateSelect, { target: { value: 'today' } })

    await waitFor(() => {
      const calls = get.mock.calls.filter((c: unknown[]) => (c[0] as string).startsWith('/posts/'))
      const latest = calls[calls.length - 1][0] as string
      expect(latest).toContain('start_date=')
      expect(latest).toContain('end_date=')
    })
  })

  it('shows custom date inputs only when the Custom preset is selected', async () => {
    mockPosts([{ ...POST_BASE, post_type: 'text', status: 'pending' }])
    harness(<ContentStudio />)

    await waitFor(() => expect(screen.getByText('Draft content')).toBeDefined())

    expect(screen.queryByLabelText('Custom start date')).toBeNull()

    const dateSelect = screen.getByLabelText('Date range preset')
    fireEvent.change(dateSelect, { target: { value: 'custom' } })

    expect(screen.getByLabelText('Custom start date')).toBeDefined()
    expect(screen.getByLabelText('Custom end date')).toBeDefined()
  })
})
