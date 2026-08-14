import { describe, expect, it, vi, afterEach, beforeEach } from 'vitest'
import type { ReactNode } from 'react'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import ContentStudio from './ContentStudio'

// Issue #1030: a text post's image could only ever be attached by a background task. These cover
// the author's half of it on the Review & Edit tab — upload, generate, remove.

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
  content: 'Draft content',
  video_url: null,
  image_url: null,
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

function mockPosts(posts: unknown[]) {
  get.mockImplementation((path: string) => {
    if (path.startsWith('/posts/')) return Promise.resolve(payload({ posts, total: posts.length, page: 1, page_size: 10 }))
    if (path.startsWith('/user/timezone')) return Promise.resolve(payload({ timezone: 'America/New_York' }))
    if (path.startsWith('/content_generation_status')) return Promise.resolve(payload(null))
    return Promise.resolve(payload({}))
  })
}

async function openPost(posts: unknown[]) {
  mockPosts(posts)
  harness(<ContentStudio />)
  await waitFor(() => expect(screen.getByText('Draft content')).toBeDefined())
  fireEvent.click(screen.getByText('Draft content'))
  await waitFor(() => expect(screen.getByDisplayValue('Draft content')).toBeDefined())
}

beforeEach(() => {
  get.mockReset()
  post.mockReset()
})

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('ContentStudio post images (issue #1030)', () => {
  it('offers upload + generate on a pending text post', async () => {
    await openPost([{ ...POST_BASE, post_type: 'text', status: 'pending' }])

    expect(screen.getByLabelText('Upload post image')).toBeDefined()
    expect(screen.getByRole('button', { name: /Generate with AI/i })).toBeDefined()
  })

  it('does not offer an image on a carousel post — the deck IS the media', async () => {
    await openPost([{ ...POST_BASE, post_type: 'carousel', status: 'pending', carousel_slides: ['Slide 1'] }])

    expect(screen.queryByLabelText('Upload post image')).toBeNull()
    expect(screen.queryByRole('button', { name: /Generate with AI/i })).toBeNull()
  })

  it('generates from the text ON SCREEN, not the stored draft', async () => {
    await openPost([{ ...POST_BASE, post_type: 'text', status: 'pending' }])
    post.mockResolvedValue(payload({ post_id: 1, image_url: 'https://api.test/api/assets?file_name=images/posts/1/img_a.png' }))

    fireEvent.change(screen.getByDisplayValue('Draft content'), { target: { value: 'Edited but unsaved' } })
    fireEvent.click(screen.getByRole('button', { name: /Generate with AI/i }))

    await waitFor(() =>
      expect(post).toHaveBeenCalledWith('/user/post/image/generate', {
        session_token: 'tok',
        post_id: 1,
        content: 'Edited but unsaved',
      })
    )
    // The new image lands on the open post without a save.
    await waitFor(() => expect(screen.getByAltText('Image on post 1')).toBeDefined())
  })

  it('surfaces the server reason when generation fails', async () => {
    await openPost([{ ...POST_BASE, post_type: 'text', status: 'pending' }])
    post.mockRejectedValue({ response: { data: { detail: 'Image generation failed' } } })

    fireEvent.click(screen.getByRole('button', { name: /Generate with AI/i }))

    await waitFor(() => expect(screen.getByText('Image generation failed')).toBeDefined())
  })

  it('removes an existing image', async () => {
    await openPost([{
      ...POST_BASE, post_type: 'text', status: 'pending',
      image_url: 'https://api.test/api/assets?file_name=images/posts/1/img_a.png',
    }])
    post.mockResolvedValue(payload({ post_id: 1, image_url: null }))

    expect(screen.getByAltText('Image on post 1')).toBeDefined()
    fireEvent.click(screen.getByRole('button', { name: /Remove image/i }))

    await waitFor(() =>
      expect(post).toHaveBeenCalledWith('/user/post/image/remove', { session_token: 'tok', post_id: 1 })
    )
    await waitFor(() => expect(screen.queryByAltText('Image on post 1')).toBeNull())
  })

  it('uploads the chosen file as multipart', async () => {
    await openPost([{ ...POST_BASE, post_type: 'text', status: 'pending' }])
    post.mockResolvedValue(payload({ post_id: 1, image_url: 'https://api.test/api/assets?file_name=images/posts/1/img_b.png' }))

    const file = new File(['bytes'], 'shot.png', { type: 'image/png' })
    fireEvent.change(screen.getByLabelText('Upload post image'), { target: { files: [file] } })

    await waitFor(() => expect(post).toHaveBeenCalled())
    const [path, form] = post.mock.calls[0]
    expect(path).toBe('/user/post/image')
    expect(form).toBeInstanceOf(FormData)
    expect((form as FormData).get('post_id')).toBe('1')
    expect((form as FormData).get('file')).toBe(file)
  })
})
