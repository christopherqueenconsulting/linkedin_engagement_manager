import { describe, expect, it, vi, afterEach, beforeEach } from 'vitest'
import type { ReactNode } from 'react'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import ContentStudio from './ContentStudio'

// Issue #774: post 33 was scheduled for 9am ET and published at 5am ET, because the wall clock the
// user typed was converted against a zone that was NOT theirs. While /user/timezone is unresolved
// the hook hands back the BROWSER's zone, so this suite pins it unresolved and asserts the studio
// refuses to convert rather than guessing.
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
  useUserTimezone: () => 'UTC',
  useUserTimezoneState: () => ({ timezone: 'UTC', isResolved: false }),
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
  content: 'Draft content',
  video_url: null,
  scheduled_time: '2026-07-28T13:00:00Z',
  carousel_slides: null,
  post_url: null,
  archetype: null,
  authenticity_score: null,
  gate_reason: null,
  rejection_reason: null,
  post_type: 'text',
  status: 'pending',
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
  get.mockImplementation((path: string) => {
    if (path.startsWith('/posts/')) {
      return Promise.resolve(payload({ posts: [POST_BASE], total: 1, page: 1, page_size: 10 }))
    }
    if (path.startsWith('/content_generation_status')) return Promise.resolve(payload(null))
    return Promise.resolve(payload({}))
  })
})

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('ContentStudio scheduling with an unresolved timezone (issue #774)', () => {
  it('blocks the bulk reschedule instead of reading the wall clock in a guessed zone', async () => {
    harness(<ContentStudio />)
    await waitFor(() => expect(screen.getByText('Draft content')).toBeDefined())

    fireEvent.click(screen.getByLabelText('Select all on this page'))

    const applyDate = screen.getByRole('button', { name: 'Apply Date' })
    const dateInput = screen.getByLabelText(/Bulk schedule date and time/)
    fireEvent.change(dateInput, { target: { value: '2026-07-28T09:00' } })

    expect((applyDate as HTMLButtonElement).disabled).toBe(true)
    fireEvent.click(applyDate)
    expect(post).not.toHaveBeenCalledWith('/posts/bulk_update/', expect.anything())
  })

  it('says the zone is still loading rather than naming the browser guess', async () => {
    harness(<ContentStudio />)
    await waitFor(() => expect(screen.getByText('Draft content')).toBeDefined())

    fireEvent.click(screen.getByLabelText('Select all on this page'))

    expect(screen.getByLabelText(/Bulk schedule date and time \(loading your timezone/)).toBeDefined()
    expect(screen.queryByLabelText(/Bulk schedule date and time \(UTC\)/)).toBeNull()
  })

  it('locks the editor time field so an unconverted edit cannot be saved', async () => {
    harness(<ContentStudio />)
    await waitFor(() => expect(screen.getByText('Draft content')).toBeDefined())

    fireEvent.click(screen.getByText('Draft content'))

    const timeInput = await waitFor(() => screen.getByLabelText(/Scheduled Time/))
    expect((timeInput as HTMLInputElement).disabled).toBe(true)
    expect((timeInput as HTMLInputElement).value).toBe('')
  })
})
