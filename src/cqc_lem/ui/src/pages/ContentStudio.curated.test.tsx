/**
 * Curated outside sources in Review (issue #2260, docs/curated-sources.md).
 *
 * A curated draft comments on someone else's content, so before the owner approves it the card has
 * to say WHAT it comments on, HOW (reshare / re-chart / link) and the exact credit it publishes
 * with — and an ordinary post must show none of that.
 */
import { describe, expect, it, vi, afterEach, beforeEach } from 'vitest'
import type { ReactNode } from 'react'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import ContentStudio from './ContentStudio'

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

vi.mock('./content/ComposePost', () => ({ default: () => null }))
vi.mock('./review/NewsletterQueue', () => ({ default: () => null }))
vi.mock('./review/ScheduledDMs', () => ({ default: () => null }))
vi.mock('./review/ConnectionRequests', () => ({ default: () => null }))
vi.mock('./review/OutreachFunnel', () => ({ default: () => null }))
vi.mock('./review/LeadsInbox', () => ({ default: () => null }))
vi.mock('./review/LeadsPipeline', () => ({ default: () => null }))
vi.mock('./review/CatchupTouches', () => ({ default: () => null }))

const POST_BASE = {
  post_id: 1,
  content: 'Jane Doe found something worth reading.',
  video_url: null,
  scheduled_time: '2026-08-10T14:00:00Z',
  post_type: 'text',
  carousel_slides: null,
  post_url: null,
  archetype: 'project_launch',
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

function mockPosts(posts: unknown[]) {
  get.mockImplementation((path: string) => {
    if (path.startsWith('/posts/')) {
      return Promise.resolve(payload({ posts, total: posts.length, page: 1, page_size: 10 }))
    }
    if (path.startsWith('/user/timezone')) return Promise.resolve(payload({ timezone: 'America/New_York' }))
    if (path.startsWith('/content_generation_status')) return Promise.resolve(payload(null))
    return Promise.resolve(payload({}))
  })
}

beforeEach(() => {
  get.mockReset()
  post.mockReset()
})

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('ContentStudio curated-source card (issue #2260)', () => {
  const CURATED = {
    source_id: 5,
    treatment: 'reshare',
    platform: 'linkedin',
    url: 'https://www.linkedin.com/feed/update/urn:li:share:9/',
    credit: 'Source: Jane Doe, LinkedIn.',
    link_only: false,
  }

  it('flags a curated draft in the list with its treatment and credit', async () => {
    mockPosts([{ ...POST_BASE, status: 'pending', manual_publish: false, curated: CURATED }])
    harness(<ContentStudio />)

    await waitFor(() =>
      expect(screen.getByText(/Curated · Reshare with commentary · Source: Jane Doe/)).toBeDefined()
    )
  })

  it('shows the source, treatment and credit in the editor before approval', async () => {
    mockPosts([{ ...POST_BASE, status: 'pending', manual_publish: false, curated: CURATED }])
    harness(<ContentStudio />)

    await waitFor(() => expect(screen.getByText('Jane Doe found something worth reading.')).toBeDefined())
    fireEvent.click(screen.getByText('Jane Doe found something worth reading.'))

    await waitFor(() =>
      expect(screen.getByText(/it publishes only after you approve it/)).toBeDefined()
    )
    expect(screen.getByText('Treatment: Reshare with commentary')).toBeDefined()
    const link = screen.getByRole('link', { name: CURATED.url })
    expect(link.getAttribute('href')).toBe(CURATED.url)
    expect(link.getAttribute('rel')).toContain('noopener')
  })

  it('never renders a non-http source URL as a link', async () => {
    mockPosts([{ ...POST_BASE, status: 'pending', manual_publish: false,
                 curated: { ...CURATED, url: 'javascript:alert(1)', treatment: 'link', link_only: true } }])
    harness(<ContentStudio />)

    await waitFor(() => expect(screen.getByText('Jane Doe found something worth reading.')).toBeDefined())
    fireEvent.click(screen.getByText('Jane Doe found something worth reading.'))
    await waitFor(() => expect(screen.getByText(/Treatment: Link post/)).toBeDefined())
    expect(screen.queryByRole('link', { name: 'javascript:alert(1)' })).toBeNull()
  })

  it('shows nothing curated on an ordinary post', async () => {
    mockPosts([{ ...POST_BASE, status: 'pending', manual_publish: false, curated: null }])
    harness(<ContentStudio />)

    await waitFor(() => expect(screen.getByText('Jane Doe found something worth reading.')).toBeDefined())
    expect(screen.queryByText(/Curated ·/)).toBeNull()
  })
})
