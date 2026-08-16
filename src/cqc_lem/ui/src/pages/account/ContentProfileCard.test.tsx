import { describe, expect, it, vi, afterEach, beforeEach } from 'vitest'
import type { ReactNode } from 'react'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import ContentProfileCard from './ContentProfileCard'

const get = vi.fn()
const put = vi.fn()
vi.mock('../../api/client', () => ({
  default: {
    get: (...args: unknown[]) => get(...args),
    put: (...args: unknown[]) => put(...args),
  },
}))
vi.mock('../../contexts/useAuth', () => ({
  useAuth: () => ({ sessionToken: 'tok', user: { email: 'jane@acme.com' } }),
}))

function settings(blog: string | null, sitemap: string | null) {
  return { data: { detail: { blog_url: blog, sitemap_url: sitemap, preferences: null, subscription: null } } }
}

function harness(ui: ReactNode) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(<QueryClientProvider client={client}>{ui}</QueryClientProvider>)
}

const blogField = () => screen.getByPlaceholderText('https://yourblog.com') as HTMLInputElement
const save = () => screen.getByRole('button', { name: /^Save$/i })

beforeEach(() => {
  get.mockReset()
  put.mockReset()
  localStorage.clear()
})
afterEach(cleanup)

describe('ContentProfileCard (issue #1574)', () => {
  it('sends an empty blog URL as null so clearing it is a real write', async () => {
    get.mockResolvedValue(settings('https://old.example.com', 'https://old.example.com/sitemap.xml'))
    put.mockResolvedValue({ data: { detail: 'ok' } })
    harness(<ContentProfileCard />)
    await waitFor(() => expect(blogField().value).toBe('https://old.example.com'))
    fireEvent.change(blogField(), { target: { value: '' } })
    fireEvent.click(save())
    await waitFor(() =>
      expect(put).toHaveBeenCalledWith('/user/', {
        session_token: 'tok',
        blog_url: null,
        sitemap_url: 'https://old.example.com/sitemap.xml',
      })
    )
  })

  it('omits a URL it has not loaded and the user has not edited', async () => {
    // The stored row has not answered yet, and localStorage only ever holds a display hint — so a
    // save now must not write the placeholder over what is stored.
    localStorage.setItem('lem_blog_url', '')
    get.mockReturnValue(new Promise(() => {}))
    put.mockResolvedValue({ data: { detail: 'ok' } })
    harness(<ContentProfileCard />)
    fireEvent.change(screen.getByPlaceholderText('https://yourblog.com/sitemap.xml'),
      { target: { value: 'https://new.example.com/sitemap.xml' } })
    fireEvent.click(save())
    await waitFor(() =>
      expect(put).toHaveBeenCalledWith('/user/', {
        session_token: 'tok',
        sitemap_url: 'https://new.example.com/sitemap.xml',
      })
    )
  })

  it('re-reads the stored row after a save instead of serving the cached one', async () => {
    get.mockResolvedValue(settings('https://old.example.com', null))
    put.mockResolvedValue({ data: { detail: 'ok' } })
    harness(<ContentProfileCard />)
    await waitFor(() => expect(get).toHaveBeenCalledTimes(1))
    fireEvent.change(blogField(), { target: { value: 'https://new.example.com' } })
    fireEvent.click(save())
    await waitFor(() => expect(screen.getByText('Settings saved!')).toBeTruthy())
    expect(get).toHaveBeenCalledTimes(2)
  })

  it('reports a failed save', async () => {
    get.mockResolvedValue(settings(null, null))
    put.mockRejectedValue(new Error('boom'))
    harness(<ContentProfileCard />)
    await waitFor(() => expect(blogField()).toBeTruthy())
    fireEvent.change(blogField(), { target: { value: 'https://new.example.com' } })
    fireEvent.click(save())
    await waitFor(() => expect(screen.getByText(/Save failed/i)).toBeTruthy())
  })
})
