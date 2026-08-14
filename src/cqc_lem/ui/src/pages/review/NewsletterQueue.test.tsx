import { describe, expect, it, vi, afterEach } from 'vitest'
import type { ReactNode } from 'react'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import NewsletterQueue from './NewsletterQueue'
import type { NewsletterEdition } from '../account/types'

const get = vi.fn()
vi.mock('../../api/client', () => ({
  default: {
    get: (...args: unknown[]) => get(...args),
    put: vi.fn(),
    post: vi.fn(),
  },
}))
vi.mock('../../contexts/useAuth', () => ({ useAuth: () => ({ user: { email: 'a@b.c' }, sessionToken: 'tok' }) }))
vi.mock('../../utils/analytics', () => ({ maskProps: (className: string) => ({ className }) }))
vi.mock('../../components/NewsletterArticlePreview', () => ({ default: () => <div data-testid="preview" /> }))

const edition = (id: number, title: string): NewsletterEdition => ({
  id,
  title,
  subtitle: `Subtitle ${id}`,
  body: `Body ${id}`,
  status: 'draft',
  scheduled_for: `2026-08-2${id}T13:00:00`,
})

/** Answers every read with the same queue — which is what a background refetch re-reads. */
const serveQueue = (editions: NewsletterEdition[]) =>
  get.mockImplementation(() =>
    Promise.resolve({ data: { detail: { editions, next_publish: null } } }))

function harness(ui: ReactNode) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return { client, ...render(<QueryClientProvider client={client}>{ui}</QueryClientProvider>) }
}

const queue = () => <NewsletterQueue userTimezone="UTC" timezoneResolved />

// The editor's labels are not wired to their controls, so the fields are read positionally:
// title, subtitle, body — in the order the editor renders them.
const boxes = () => screen.getAllByRole('textbox') as HTMLInputElement[]
const titleBox = () => boxes()[0]
const bodyBox = () => boxes()[2]

afterEach(() => { cleanup(); get.mockReset() })

describe('NewsletterQueue editor', () => {
  it('opens on the soonest queued edition', async () => {
    serveQueue([edition(1, 'First up'), edition(2, 'Later')])
    harness(queue())
    await waitFor(() => expect(titleBox().value).toBe('First up'))
    expect(bodyBox().value).toBe('Body 1')
  })

  it('keeps an edit in progress when the queue is refetched underneath it', async () => {
    serveQueue([edition(1, 'First up'), edition(2, 'Later')])
    const { client } = harness(queue())
    await waitFor(() => expect(titleBox().value).toBe('First up'))

    fireEvent.change(titleBox(), { target: { value: 'My own headline' } })
    await client.invalidateQueries({ queryKey: ['newsletter-queue'] })

    // The refetch answers with the server's title again — the edit has to survive it, or a poll
    // landing mid-sentence would silently discard what the user typed.
    await waitFor(() => expect(titleBox().value).toBe('My own headline'))
  })

  it('shows the other edition as the server has it, never the edit made on this one', async () => {
    serveQueue([edition(1, 'First up'), edition(2, 'Later')])
    harness(queue())
    await waitFor(() => expect(titleBox().value).toBe('First up'))

    fireEvent.change(titleBox(), { target: { value: 'My own headline' } })
    fireEvent.click(screen.getByText('Later'))

    await waitFor(() => expect(titleBox().value).toBe('Later'))
    expect(bodyBox().value).toBe('Body 2')

    // …and coming back shows the edit again, still attached to the edition it was made on.
    fireEvent.click(screen.getByText('First up'))
    await waitFor(() => expect(titleBox().value).toBe('My own headline'))
  })

  it('falls back to the soonest remaining draft when the selected one leaves the queue', async () => {
    serveQueue([edition(1, 'First up'), edition(2, 'Later')])
    const { client } = harness(queue())
    await waitFor(() => expect(titleBox().value).toBe('First up'))

    fireEvent.click(screen.getByText('Later'))
    await waitFor(() => expect(titleBox().value).toBe('Later'))

    // Edition 2 is approved away by the publish beat.
    serveQueue([edition(1, 'First up')])
    await client.invalidateQueries({ queryKey: ['newsletter-queue'] })

    await waitFor(() => expect(titleBox().value).toBe('First up'))
  })
})
