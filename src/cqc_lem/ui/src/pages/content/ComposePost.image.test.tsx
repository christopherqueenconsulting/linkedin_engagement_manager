import { describe, expect, it, vi, afterEach, beforeEach } from 'vitest'
import type { ReactNode } from 'react'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import ComposePost from './ComposePost'

// Issue #1030: there is no post row while the author is composing, so an uploaded/generated image
// comes back as a PREVIEW url that rides along in the schedule payload.
const get = vi.fn()
const post = vi.fn()

vi.mock('../../api/client', () => ({
  default: {
    get: (...args: unknown[]) => get(...args),
    post: (...args: unknown[]) => post(...args),
  },
}))

vi.mock('../../contexts/AuthContext', () => ({
  useAuth: () => ({ user: { email: 'test@example.com', userId: 1 }, sessionToken: 'tok' }),
}))

vi.mock('../../hooks/useUserTimezone', () => ({
  useUserTimezone: () => 'America/New_York',
  useUserTimezoneState: () => ({ timezone: 'America/New_York', isResolved: true }),
}))

vi.mock('../../utils/analytics', () => ({
  maskProps: (className: string) => ({ className }),
}))

vi.mock('../../components/LinkedInPostPreview', () => ({ default: () => null }))
vi.mock('../../components/TopicAuthorityMeter', () => ({ default: () => null }))

const PREVIEW_URL = 'https://api.test/api/assets?file_name=images/post_previews/1/img_a.png'

let client: QueryClient

function tree(ui: ReactNode) {
  return (
    <MemoryRouter>
      <QueryClientProvider client={client}>{ui}</QueryClientProvider>
    </MemoryRouter>
  )
}

beforeEach(() => {
  get.mockReset()
  post.mockReset()
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  get.mockResolvedValue({ data: { detail: { templates: [] } } })
})

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

const timeInput = (c: HTMLElement) => c.querySelector('input[type="datetime-local"]') as HTMLInputElement

describe('ComposePost images on a text post (issue #1030)', () => {
  it('offers upload + generate on TEXT and hides them on CAROUSEL', () => {
    render(tree(<ComposePost />))

    expect(screen.getByLabelText('Upload post image')).toBeDefined()
    fireEvent.click(screen.getByRole('button', { name: 'CAROUSEL' }))
    expect(screen.queryByLabelText('Upload post image')).toBeNull()
  })

  it('refuses to generate before anything is written', async () => {
    render(tree(<ComposePost />))

    fireEvent.click(screen.getByRole('button', { name: /Generate with AI/i }))

    await waitFor(() => expect(screen.getByText(/Write your post first/i)).toBeDefined())
    expect(post).not.toHaveBeenCalled()
  })

  it('carries a generated preview image into the scheduled post', async () => {
    const { container } = render(tree(<ComposePost />))
    post.mockResolvedValue({ data: { detail: { post_id: null, image_url: PREVIEW_URL } } })

    fireEvent.change(screen.getByPlaceholderText('What would you like to share?'), {
      target: { value: 'Hello world' },
    })
    fireEvent.click(screen.getByRole('button', { name: /Generate with AI/i }))

    await waitFor(() =>
      expect(post).toHaveBeenCalledWith('/user/post/image/generate', {
        session_token: 'tok',
        content: 'Hello world',
      })
    )
    await waitFor(() => expect(screen.getByAltText('Post image')).toBeDefined())

    fireEvent.change(timeInput(container), { target: { value: '2026-07-28T09:00' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save Draft' }))

    await waitFor(() =>
      expect(post).toHaveBeenCalledWith('/schedule_post/', expect.objectContaining({
        image_url: PREVIEW_URL,
      }))
    )
  })

  it('surfaces the server reason when an upload is rejected', async () => {
    render(tree(<ComposePost />))
    post.mockRejectedValue({ response: { data: { detail: 'Use a PNG or JPG image' } } })

    const file = new File(['bytes'], 'shot.gif', { type: 'image/gif' })
    fireEvent.change(screen.getByLabelText('Upload post image'), { target: { files: [file] } })

    await waitFor(() => expect(screen.getByText('Use a PNG or JPG image')).toBeDefined())
    expect(screen.queryByAltText('Post image')).toBeNull()
  })
})
