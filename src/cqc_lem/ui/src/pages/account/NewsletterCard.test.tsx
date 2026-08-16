import { describe, expect, it, vi, afterEach, beforeEach } from 'vitest'
import type { ReactNode } from 'react'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import NewsletterCard from './NewsletterCard'

const get = vi.fn()
const put = vi.fn()
vi.mock('../../api/client', () => ({
  default: {
    get: (...args: unknown[]) => get(...args),
    put: (...args: unknown[]) => put(...args),
  },
}))
vi.mock('../../contexts/useAuth', () => ({ useAuth: () => ({ sessionToken: 'tok' }) }))

const SETTINGS = {
  enabled: true,
  title: 'Weekly Wins',
  topic: 'reach',
  cadence: 'weekly',
  align_with_blog: true,
  newsletter_url: null,
  last_published_at: null,
  publish_day: 1,
  publish_hour: 9,
  generate_lead_days: 3,
  max_queued_drafts: 1,
  invite_connections_enabled: false,
  max_invites_per_run: 50,
  cover_image_auto: false,
  auto_publish_newsletters: false,
}

const SUBSCRIBERS = {
  latest: 12,
  history: [],
  attribution: { window_days: 30, lead_magnet_dms: 0, newsletter_links: null },
}

/** Serve the two queries the card makes; `settings` is laid over the stored row. */
const serve = (settings: Partial<typeof SETTINGS> = {}) =>
  get.mockImplementation((...args: unknown[]) =>
    Promise.resolve({
      data: {
        detail: String(args[0]).includes('newsletter-subscribers')
          ? SUBSCRIBERS
          : { ...SETTINGS, ...settings },
      },
    }))

function harness(ui: ReactNode) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return {
    client,
    ...render(
      <QueryClientProvider client={client}>
        <MemoryRouter>{ui}</MemoryRouter>
      </QueryClientProvider>),
  }
}

const autoPublishToggle = () => screen.getByRole('switch', { name: 'Publish drafts without my approval' })
const save = () => screen.getByRole('button', { name: /Save Newsletter Settings/i })

beforeEach(() => { put.mockResolvedValue({ data: { detail: 'ok' } }) })
afterEach(() => { cleanup(); get.mockReset(); put.mockReset() })

describe('NewsletterCard — auto-publish toggle (issue #1135)', () => {
  it('renders the control OFF for an account that requires approval', async () => {
    serve({ auto_publish_newsletters: false })
    harness(<NewsletterCard />)
    await waitFor(() => expect(autoPublishToggle().getAttribute('aria-checked')).toBe('false'))
  })

  it('renders the control ON for an account that was backfilled to auto-publishing', async () => {
    serve({ auto_publish_newsletters: true })
    harness(<NewsletterCard />)
    await waitFor(() => expect(autoPublishToggle().getAttribute('aria-checked')).toBe('true'))
  })

  it('saves the flipped value — the whole point of the control existing', async () => {
    serve({ auto_publish_newsletters: false })
    harness(<NewsletterCard />)
    await waitFor(() => expect(autoPublishToggle().getAttribute('aria-checked')).toBe('false'))

    fireEvent.click(autoPublishToggle())
    expect(autoPublishToggle().getAttribute('aria-checked')).toBe('true')

    fireEvent.click(save())
    await waitFor(() => expect(put).toHaveBeenCalled())
    expect(put.mock.calls[0][1]).toMatchObject({ auto_publish_newsletters: true })
  })

  it('round-trips: the saved value is what the card shows after the refetch', async () => {
    serve({ auto_publish_newsletters: false })
    harness(<NewsletterCard />)
    await waitFor(() => expect(autoPublishToggle().getAttribute('aria-checked')).toBe('false'))

    fireEvent.click(autoPublishToggle())
    serve({ auto_publish_newsletters: true })     // the API now stores what was just saved
    fireEvent.click(save())

    await waitFor(() => expect(autoPublishToggle().getAttribute('aria-checked')).toBe('true'))
  })

  it('carries every other setting through the same PUT, so turning it on blanks nothing', async () => {
    serve({ auto_publish_newsletters: false })
    harness(<NewsletterCard />)
    await waitFor(() => expect(autoPublishToggle().getAttribute('aria-checked')).toBe('false'))

    fireEvent.click(autoPublishToggle())
    fireEvent.click(save())
    await waitFor(() => expect(put).toHaveBeenCalled())
    expect(put.mock.calls[0][1]).toMatchObject({
      title: 'Weekly Wins', cadence: 'weekly', max_queued_drafts: 1, cover_image_auto: false,
    })
  })
})
