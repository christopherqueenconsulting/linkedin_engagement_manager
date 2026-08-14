import { describe, expect, it, vi, afterEach, beforeEach } from 'vitest'
import type { ReactNode } from 'react'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import LinkedInDisplayNameCard from './LinkedInDisplayNameCard'

const get = vi.fn()
const put = vi.fn()
vi.mock('../../api/client', () => ({
  default: {
    get: (...args: unknown[]) => get(...args),
    put: (...args: unknown[]) => put(...args),
  },
}))
vi.mock('../../contexts/useAuth', () => ({ useAuth: () => ({ sessionToken: 'tok' }) }))

function payload(saved: string | null, scraped: string | null) {
  return { data: { detail: { linkedin_display_name: saved, profile_full_name: scraped } } }
}

function harness(ui: ReactNode) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(<QueryClientProvider client={client}>{ui}</QueryClientProvider>)
}

const field = () => screen.getByLabelText(/Full name on LinkedIn/i) as HTMLInputElement

beforeEach(() => {
  get.mockReset()
  put.mockReset()
})
afterEach(cleanup)

describe('LinkedInDisplayNameCard (issue #731)', () => {
  it('loads the saved name and says the field is required', async () => {
    get.mockResolvedValue(payload('Christopher Queen', 'Christopher Queen'))
    harness(<LinkedInDisplayNameCard />)
    await waitFor(() => expect(field().value).toBe('Christopher Queen'))
    expect(screen.getByText('Required')).toBeTruthy()
    // Exact-match instruction — a name that differs from LinkedIn's reads as UNKNOWN in production.
    expect(screen.getByText(/exactly as it appears/i)).toBeTruthy()
  })

  it('warns while the required field is empty', async () => {
    get.mockResolvedValue(payload(null, null))
    harness(<LinkedInDisplayNameCard />)
    await waitFor(() => expect(screen.getByText(/DM follow-ups stay paused/i)).toBeTruthy())
  })

  it('offers the scraped profile name as a one-click suggestion', async () => {
    get.mockResolvedValue(payload(null, 'Christopher Queen'))
    harness(<LinkedInDisplayNameCard />)
    await waitFor(() => expect(screen.getByText(/Your scraped profile reads/)).toBeTruthy())
    fireEvent.click(screen.getByText('Use this'))
    expect(field().value).toBe('Christopher Queen')
  })

  it('saves the typed name', async () => {
    get.mockResolvedValue(payload(null, null))
    put.mockResolvedValue({ data: { detail: 'ok' } })
    harness(<LinkedInDisplayNameCard />)
    await waitFor(() => expect(field()).toBeTruthy())
    fireEvent.change(field(), { target: { value: 'Christopher Queen' } })
    fireEvent.click(screen.getByRole('button', { name: /Save Name/i }))
    await waitFor(() =>
      expect(put).toHaveBeenCalledWith('/user/linkedin-display-name', {
        session_token: 'tok',
        linkedin_display_name: 'Christopher Queen',
      })
    )
  })

  it('cannot save an empty name', async () => {
    get.mockResolvedValue(payload(null, null))
    harness(<LinkedInDisplayNameCard />)
    await waitFor(() => expect(field()).toBeTruthy())
    fireEvent.change(field(), { target: { value: '   ' } })
    const save = screen.getByRole('button', { name: /Save Name/i }) as HTMLButtonElement
    expect(save.disabled).toBe(true)
    expect(put).not.toHaveBeenCalled()
  })
})
