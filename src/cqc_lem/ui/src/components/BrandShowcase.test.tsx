import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen, waitFor } from '@testing-library/react'
import BrandShowcase from './BrandShowcase'
import { useFeatureFlag } from '../hooks/useFeatureFlags'

const get = vi.fn()

vi.mock('../api/client', () => ({
  default: {
    get: (...args: unknown[]) => get(...args),
  },
}))

vi.mock('../hooks/useFeatureFlags', () => ({
  FLAGS: { brandShowcase: 'brand-showcase-enabled' },
  useFeatureFlag: vi.fn(),
}))

function payload(posts: unknown[]) {
  return { data: { detail: { posts } } }
}

beforeEach(() => {
  get.mockReset()
})

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('BrandShowcase (issue #1299)', () => {
  it('renders nothing when the feature flag is off', () => {
    vi.mocked(useFeatureFlag).mockReturnValue(false)
    const { container } = render(<BrandShowcase />)
    expect(container.innerHTML).toBe('')
    expect(get).not.toHaveBeenCalled()
  })

  it('renders nothing on an empty payload', () => {
    vi.mocked(useFeatureFlag).mockReturnValue(true)
    get.mockResolvedValue(payload([]))
    const { container } = render(<BrandShowcase />)
    expect(container.innerHTML).toBe('')
  })

  it('renders nothing while loading', () => {
    vi.mocked(useFeatureFlag).mockReturnValue(true)
    get.mockReturnValue(new Promise(() => undefined))
    const { container } = render(<BrandShowcase />)
    expect(container.innerHTML).toBe('')
  })

  it('renders nothing when the API call fails', () => {
    vi.mocked(useFeatureFlag).mockReturnValue(true)
    get.mockRejectedValue(new Error('network'))
    const { container } = render(<BrandShowcase />)
    expect(container.innerHTML).toBe('')
  })

  it('renders real posts with stored engagement numbers', async () => {
    vi.mocked(useFeatureFlag).mockReturnValue(true)
    get.mockResolvedValue(payload([
      {
        id: 1,
        content: 'First post written by LEM.',
        post_type: 'text',
        published_at: '2026-08-01T12:00:00Z',
        post_url: 'https://linkedin.com/posts/lem-1',
        reactions: 42,
        comments: 7,
        reposts: 3,
        impressions: 1200,
        saves: 1,
      },
    ]))

    render(<BrandShowcase />)

    await waitFor(() => expect(screen.getByText('Made with LEM')).toBeTruthy())
    expect(screen.getByText('First post written by LEM.')).toBeTruthy()
    expect(screen.getByText('42')).toBeTruthy()
    expect(screen.getByText('7')).toBeTruthy()
    expect(screen.getByText('3')).toBeTruthy()
    expect(screen.getByText('View on LinkedIn →')).toBeTruthy()
    // Impressions and saves are not rendered, so this asserts no client-side derivation is added.
    expect(screen.queryByText('1,200')).toBeNull()
    expect(screen.queryByText('1')).toBeNull()
  })

  it('displays em-dash for missing stats instead of fabricating zero', async () => {
    vi.mocked(useFeatureFlag).mockReturnValue(true)
    get.mockResolvedValue(payload([
      {
        id: 2,
        content: 'No stats yet.',
        post_type: 'text',
        published_at: null,
        post_url: null,
        reactions: null,
        comments: null,
        reposts: null,
        impressions: null,
        saves: null,
      },
    ]))

    render(<BrandShowcase />)

    await waitFor(() => expect(screen.getByText('No stats yet.')).toBeTruthy())
    const dashes = screen.getAllText('—')
    expect(dashes.length).toBeGreaterThanOrEqual(3)
  })

  it('truncates long content without changing the stored value', async () => {
    vi.mocked(useFeatureFlag).mockReturnValue(true)
    const longContent = 'a '.repeat(200)
    get.mockResolvedValue(payload([
      {
        id: 3,
        content: longContent,
        post_type: 'text',
        published_at: null,
        post_url: null,
        reactions: 1,
        comments: null,
        reposts: null,
        impressions: null,
        saves: null,
      },
    ]))

    const { container } = render(<BrandShowcase />)
    await waitFor(() => expect(container.textContent).toContain('…'))
    expect(container.textContent).not.toContain(longContent)
  })
})
