/**
 * An AI agent's draft that LEM is still writing (MCP W2b, issue #2689).
 *
 * The server refuses to approve, re-score or bulk-approve a post whose body is still the agent
 * placeholder, and refuses a stale save over a draft LEM has since written. The author must be told
 * why, in the server's own words, instead of the click silently doing nothing; and a bulk approve
 * approves the rest of the batch and says which posts it left alone.
 */
import { describe, expect, it, vi, afterEach, beforeEach } from 'vitest'
import type { ReactNode } from 'react'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import ContentStudio from './ContentStudio'
import { recordPostApproval } from '../utils/analytics'

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

const PLACEHOLDER = '[LEM is writing this draft for your AI agent. It will be ready to review in a few minutes.]'
const STILL_WRITING = 'LEM is still writing this draft for your AI agent; it cannot be approved yet.'

const POST_BASE = {
  video_url: null,
  scheduled_time: '2026-08-10T14:00:00Z',
  post_type: 'text',
  carousel_slides: null,
  post_url: null,
  archetype: null,
  authenticity_score: null,
  gate_reason: null,
  rejection_reason: null,
  status: 'pending',
}

function payload(data: unknown) {
  return { data: { detail: data } }
}

function refusal(status: number, code: string, message: string) {
  return Object.assign(new Error(`Request failed with status code ${status}`), {
    response: { status, data: { detail: { code, message } } },
  })
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

async function openEditor(text: string) {
  await waitFor(() => expect(screen.getByText(text)).toBeDefined())
  fireEvent.click(screen.getByText(text))
  await waitFor(() => expect(screen.getByRole('button', { name: /Save Changes/i })).toBeDefined())
}

beforeEach(() => {
  get.mockReset()
  post.mockReset()
})

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe("ContentStudio and an AI agent's unwritten draft (issue #2689)", () => {
  it("shows the server's reason when Save is refused because LEM is still writing it", async () => {
    mockPosts([{ ...POST_BASE, post_id: 1, content: PLACEHOLDER }])
    post.mockRejectedValue(refusal(409, 'agent_draft_not_written', STILL_WRITING))
    harness(<ContentStudio />)

    await openEditor(PLACEHOLDER)
    fireEvent.click(screen.getByRole('button', { name: /Save Changes/i }))

    await waitFor(() => expect(screen.getByRole('alert').textContent).toBe(STILL_WRITING))
    expect(screen.getByRole('button', { name: /Save Changes/i })).toBeDefined() // editor stays open
  })

  it('shows the stale-save reason, and a generic one for any other failure', async () => {
    mockPosts([{ ...POST_BASE, post_id: 1, content: PLACEHOLDER }])
    post.mockRejectedValueOnce(refusal(409, 'agent_placeholder_stale',
      'LEM has written this draft since you opened it. Reload it before saving.'))
    harness(<ContentStudio />)

    await openEditor(PLACEHOLDER)
    fireEvent.click(screen.getByRole('button', { name: /Save Changes/i }))
    await waitFor(() => expect(screen.getByRole('alert').textContent).toMatch(/Reload it/))

    post.mockRejectedValueOnce(new Error('Network Error'))
    fireEvent.click(screen.getByRole('button', { name: /Save Changes/i }))
    await waitFor(() =>
      expect(screen.getByRole('alert').textContent).toBe('Could not save this post — please try again.'))
  })

  it("shows the server's reason when a re-score is refused", async () => {
    mockPosts([{ ...POST_BASE, post_id: 1, content: PLACEHOLDER }])
    post.mockImplementation((path: string) =>
      path.startsWith('/update_post/')
        ? Promise.resolve(payload('Post updated successful'))
        : Promise.reject(refusal(409, 'agent_draft_not_written', STILL_WRITING)))
    harness(<ContentStudio />)

    await openEditor(PLACEHOLDER)
    fireEvent.click(screen.getByRole('button', { name: /Save & re-score/i }))

    await waitFor(() => expect(screen.getByText(STILL_WRITING)).toBeDefined())
  })

  it('a bulk approve reports the skipped drafts and records only the posts it approved', async () => {
    mockPosts([
      { ...POST_BASE, post_id: 1, content: 'A human draft.' },
      { ...POST_BASE, post_id: 2, content: PLACEHOLDER },
    ])
    post.mockResolvedValue({
      data: {
        detail: 'Posts updated successfully',
        skipped: [{ post_id: 2, code: 'agent_draft_not_written' }],
      },
    })
    harness(<ContentStudio />)

    await waitFor(() => expect(screen.getByText('A human draft.')).toBeDefined())
    fireEvent.click(screen.getByLabelText(/Select all on this page/i))
    // The bulk status select defaults to APPROVED.
    fireEvent.click(screen.getByRole('button', { name: /Apply Status/i }))

    await waitFor(() =>
      expect(screen.getByRole('status').textContent).toMatch(/1 post was left unchanged/))
    expect(post).toHaveBeenCalledWith('/posts/bulk_update/',
      { post_ids: [1, 2], status: 'approved', session_token: 'tok' })
    const approved = vi.mocked(recordPostApproval).mock.calls.map((c) => (c[0] as { post_id: number }).post_id)
    expect(approved).toEqual([1])
  })

  it("a bulk approve refused outright shows the server's reason", async () => {
    mockPosts([{ ...POST_BASE, post_id: 2, content: PLACEHOLDER }])
    post.mockRejectedValue(refusal(409, 'agent_draft_not_written',
      'LEM is still writing these drafts for your AI agent; they cannot be approved yet.'))
    harness(<ContentStudio />)

    await waitFor(() => expect(screen.getByText(PLACEHOLDER)).toBeDefined())
    fireEvent.click(screen.getByLabelText(/Select all on this page/i))
    // The bulk status select defaults to APPROVED.
    fireEvent.click(screen.getByRole('button', { name: /Apply Status/i }))

    await waitFor(() =>
      expect(screen.getByRole('status').textContent).toMatch(/still writing these drafts/))
  })
})
