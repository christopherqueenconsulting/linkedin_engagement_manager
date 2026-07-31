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

vi.mock('../contexts/AuthContext', () => ({
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

// ComposePost and heavy sub-pages are not under test here.
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

describe('ContentStudio regenerate-with-guidance (issue #778)', () => {
  it('shows the guidance section for a pending text post', async () => {
    mockPosts([{ ...POST_BASE, post_type: 'text', status: 'pending' }])
    harness(<ContentStudio />)

    await waitFor(() => expect(screen.getByText('Draft content')).toBeDefined())
    fireEvent.click(screen.getByText('Draft content'))

    await waitFor(() =>
      expect(screen.getByRole('button', { name: /Re-generate Post/i })).toBeDefined()
    )
  })

  it('shows the guidance section for a rejected text post', async () => {
    mockPosts([{ ...POST_BASE, post_type: 'text', status: 'rejected', rejection_reason: 'Too salesy' }])
    harness(<ContentStudio />)

    await waitFor(() => expect(screen.getByText('Draft content')).toBeDefined())
    fireEvent.click(screen.getByText('Draft content'))

    await waitFor(() =>
      expect(screen.getByRole('button', { name: /Re-generate Post/i })).toBeDefined()
    )
    expect(screen.getByRole('button', { name: /Regenerate using this feedback/i })).toBeDefined()
  })

  it('hides the guidance section for an approved text post', async () => {
    mockPosts([{ ...POST_BASE, post_type: 'text', status: 'approved' }])
    harness(<ContentStudio />)

    await waitFor(() => expect(screen.getByText('Draft content')).toBeDefined())
    fireEvent.click(screen.getByText('Draft content'))

    await waitFor(() => expect(screen.getByDisplayValue('Draft content')).toBeDefined())
    expect(screen.queryByRole('button', { name: /Re-generate Post/i })).toBeNull()
  })

  it('hides the guidance section for a pending non-text post', async () => {
    mockPosts([{ ...POST_BASE, post_type: 'carousel', status: 'pending', carousel_slides: ['Slide 1'] }])
    harness(<ContentStudio />)

    await waitFor(() => expect(screen.getByText('Draft content')).toBeDefined())
    fireEvent.click(screen.getByText('Draft content'))

    await waitFor(() => expect(screen.getByDisplayValue('Draft content')).toBeDefined())
    expect(screen.queryByRole('button', { name: /Re-generate Post/i })).toBeNull()
  })

  it('sends guidance to the regenerate endpoint', async () => {
    mockPosts([{ ...POST_BASE, post_type: 'text', status: 'pending' }])
    post.mockResolvedValue(payload({}))
    harness(<ContentStudio />)

    await waitFor(() => expect(screen.getByText('Draft content')).toBeDefined())
    fireEvent.click(screen.getByText('Draft content'))

    const textarea = await screen.findByPlaceholderText(/lead with a client result/i)
    fireEvent.change(textarea, { target: { value: 'Make it punchier' } })
    fireEvent.click(screen.getByRole('button', { name: /Re-generate Post/i }))

    await waitFor(() =>
      expect(post).toHaveBeenCalledWith('/user/post/regenerate', {
        session_token: 'tok',
        post_id: 1,
        guidance: 'Make it punchier',
      })
    )
  })
})
