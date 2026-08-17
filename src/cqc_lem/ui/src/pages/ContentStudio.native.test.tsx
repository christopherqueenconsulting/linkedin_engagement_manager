/**
 * Occasion / milestone drafts in Review (issue #1074).
 *
 * LinkedIn's occasion composer has no API, so the one thing this state must get right is that the
 * author is told to publish it themselves and given a way to record that they did — an occasion
 * draft that looks like every other scheduled post silently never ships.
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
  content: 'We shipped it.',
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

describe('ContentStudio native-publish state (issue #1074)', () => {
  it('flags an occasion draft in the list', async () => {
    mockPosts([{ ...POST_BASE, status: 'approved', manual_publish: true }])
    harness(<ContentStudio />)

    await waitFor(() => expect(screen.getByText('POST NATIVELY')).toBeDefined())
  })

  it('shows the copy + mark-as-posted actions in the editor', async () => {
    mockPosts([{ ...POST_BASE, status: 'approved', manual_publish: true }])
    harness(<ContentStudio />)

    await waitFor(() => expect(screen.getByText('We shipped it.')).toBeDefined())
    fireEvent.click(screen.getByText('We shipped it.'))

    await waitFor(() => expect(screen.getByText('Post natively on LinkedIn')).toBeDefined())
    expect(screen.getByRole('button', { name: /Copy post text/i })).toBeDefined()
    expect(screen.getByRole('button', { name: /I posted this/i })).toBeDefined()
  })

  it('marks the post published through the dedicated endpoint', async () => {
    mockPosts([{ ...POST_BASE, status: 'approved', manual_publish: true }])
    post.mockResolvedValue(payload('Post marked as published'))
    harness(<ContentStudio />)

    await waitFor(() => expect(screen.getByText('We shipped it.')).toBeDefined())
    fireEvent.click(screen.getByText('We shipped it.'))
    await waitFor(() => expect(screen.getByRole('button', { name: /I posted this/i })).toBeDefined())
    fireEvent.click(screen.getByRole('button', { name: /I posted this/i }))

    await waitFor(() =>
      expect(post).toHaveBeenCalledWith('/user/post/mark-posted', {
        session_token: 'tok',
        post_id: 1,
      })
    )
  })

  it('warns instead of telling the author to paste an unconfirmed publish again (issue #1088)', async () => {
    // A native-publish attempt whose outcome never answered lands the draft at 'error'. The post
    // may already be live, and an occasion post published twice is public and un-deletable — so
    // this state must not carry the ordinary "paste the text below" instruction.
    mockPosts([{ ...POST_BASE, status: 'error', manual_publish: true }])
    harness(<ContentStudio />)

    await waitFor(() => expect(screen.getByText('We shipped it.')).toBeDefined())
    fireEvent.click(screen.getByText('We shipped it.'))

    await waitFor(() => expect(screen.getByText('Post natively on LinkedIn')).toBeDefined())
    expect(screen.getByText(/may already be on LinkedIn/i)).toBeDefined()
    expect(screen.queryByText(/paste the text\s+below/i)).toBeNull()
    // The way OUT of the state is still there — that is the whole point of holding it for a human.
    expect(screen.getByRole('button', { name: /I posted this/i })).toBeDefined()
  })

  it('leaves an ordinary post alone', async () => {
    mockPosts([{ ...POST_BASE, status: 'approved', manual_publish: false }])
    harness(<ContentStudio />)

    await waitFor(() => expect(screen.getByText('We shipped it.')).toBeDefined())
    fireEvent.click(screen.getByText('We shipped it.'))

    await waitFor(() => expect(screen.getByDisplayValue('We shipped it.')).toBeDefined())
    expect(screen.queryByText('POST NATIVELY')).toBeNull()
    expect(screen.queryByText('Post natively on LinkedIn')).toBeNull()
  })
})
