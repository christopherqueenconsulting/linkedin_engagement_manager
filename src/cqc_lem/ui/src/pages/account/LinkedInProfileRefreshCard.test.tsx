import { describe, expect, it, vi, afterEach, beforeEach } from 'vitest'
import type { ReactNode } from 'react'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import LinkedInProfileRefreshCard from './LinkedInProfileRefreshCard'

const get = vi.fn()
const post = vi.fn()
vi.mock('../../api/client', () => ({
  default: {
    get: (...args: unknown[]) => get(...args),
    post: (...args: unknown[]) => post(...args),
  },
}))
vi.mock('../../contexts/AuthContext', () => ({ useAuth: () => ({ sessionToken: 'tok' }) }))

function profile(refreshAvailableIn: number) {
  return {
    data: {
      detail: {
        linkedin_profile_url: 'https://www.linkedin.com/in/jane/',
        refresh_available_in_seconds: refreshAvailableIn,
      },
    },
  }
}

function harness(ui: ReactNode) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(<QueryClientProvider client={client}>{ui}</QueryClientProvider>)
}

const button = () => screen.getByTestId('refresh-profile-data') as HTMLButtonElement

beforeEach(() => {
  get.mockReset()
  post.mockReset()
})
afterEach(cleanup)

describe('LinkedInProfileRefreshCard (issue #1076)', () => {
  it('queues a refresh when the window is open', async () => {
    get.mockResolvedValue(profile(0))
    post.mockResolvedValue({ data: { detail: { queued: true, reason: 'queued', retry_after_seconds: 0 } } })
    harness(<LinkedInProfileRefreshCard />)
    await waitFor(() => expect(button().disabled).toBe(false))
    fireEvent.click(button())
    await waitFor(() =>
      expect(post).toHaveBeenCalledWith('/user/linkedin-profile/refresh', { session_token: 'tok' })
    )
    await waitFor(() => expect(screen.getByText(/Refreshing/i)).toBeTruthy())
  })

  it('stays disabled after a reload when the window is already spent', async () => {
    // The server owns the window, so a user who pressed the button, closed the tab and came back
    // must not be shown an armed button that will only answer "already refreshed today".
    get.mockResolvedValue(profile(7200))
    harness(<LinkedInProfileRefreshCard />)
    await waitFor(() => expect(button().disabled).toBe(true))
    expect(button().textContent).toMatch(/Refreshed today/i)
    expect(post).not.toHaveBeenCalled()
  })

  it('reports a spent window as a no-op rather than a failure', async () => {
    get.mockResolvedValue(profile(0))
    post.mockResolvedValue({
      data: { detail: { queued: false, reason: 'already_refreshed_today', retry_after_seconds: 3600 } },
    })
    harness(<LinkedInProfileRefreshCard />)
    await waitFor(() => expect(button().disabled).toBe(false))
    fireEvent.click(button())
    await waitFor(() => expect(screen.getByText(/Already refreshed today/i)).toBeTruthy())
  })

  it('surfaces a failed request', async () => {
    get.mockResolvedValue(profile(0))
    post.mockRejectedValue(new Error('boom'))
    harness(<LinkedInProfileRefreshCard />)
    await waitFor(() => expect(button().disabled).toBe(false))
    fireEvent.click(button())
    await waitFor(() => expect(screen.getByText(/Could not start the refresh/i)).toBeTruthy())
  })

  it('treats a payload with no window field as refreshable', async () => {
    // An older API build (or a cached bundle talking to one) omits the field entirely; the button
    // must stay usable rather than read as permanently spent.
    get.mockResolvedValue({ data: { detail: { linkedin_profile_url: null } } })
    harness(<LinkedInProfileRefreshCard />)
    await waitFor(() => expect(button().disabled).toBe(false))
  })
})
