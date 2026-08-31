import { describe, expect, it, vi, afterEach } from 'vitest'
import type { ReactNode } from 'react'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import NewsletterQueue from './NewsletterQueue'
import type { NewsletterEdition } from '../account/types'

const get = vi.fn()
const post = vi.fn()
vi.mock('../../api/client', () => ({
  default: {
    get: (...args: unknown[]) => get(...args),
    put: vi.fn(),
    post: (...args: unknown[]) => post(...args),
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

/** Answers every read with the same queue — which is what a background refetch re-reads.
 *  `autoPublish` defaults to true: that is the shape of an account that existed before issue
 *  #1135 (every stored row was backfilled to it), so these cases keep reading as they did. */
const serveQueue = (editions: NewsletterEdition[], autoPublish = true) =>
  get.mockImplementation(() =>
    Promise.resolve({
      data: { detail: { editions, next_publish: null, auto_publish_newsletters: autoPublish } },
    }))

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

afterEach(() => { cleanup(); get.mockReset(); post.mockReset() })

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

  // A cover decision writes the returned cover fields straight into the local edits so the panel
  // answers instantly. That override must not outlive the refetch it triggers: the cover the
  // editor shows — and the review status next to it — is the API row's, never this session's.
  it('hands the cover back to the API row once the decision refetch lands', async () => {
    const withCover = (url: string, status: 'pending_review' | 'approved'): NewsletterEdition => ({
      ...edition(1, 'First up'),
      cover_image_url: url,
      cover_image_source: 'ai',
      cover_image_status: status,
    })
    serveQueue([withCover('https://cdn.test/old.png', 'pending_review')])
    harness(queue())
    await waitFor(() => expect(screen.getByText('NEEDS YOUR APPROVAL')).toBeTruthy())

    // A regeneration lands a different cover — still unapproved — while this tab is deciding on
    // the one it is looking at.
    serveQueue([withCover('https://cdn.test/new.png', 'pending_review')])
    post.mockResolvedValue({
      data: {
        detail: {
          cover_image_url: 'https://cdn.test/old.png',
          cover_image_source: 'ai',
          cover_image_status: 'approved',
        },
      },
    })
    // By role: the editor's warning copy names the control too, so the bare text is ambiguous.
    fireEvent.click(screen.getByRole('button', { name: 'Approve cover' }))

    await waitFor(() =>
      expect(screen.getByAltText('Newsletter cover').getAttribute('src'))
        .toBe('https://cdn.test/new.png'))
    expect(screen.getByText('NEEDS YOUR APPROVAL')).toBeTruthy()
    expect(screen.queryByText('PUBLISHES WITH THIS EDITION')).toBeNull()
  })
})

// Issue #1432: no generated cover in production was EVER approved, so every edition shipped
// cover-less. The approve control was always there — nothing outside the open editor said it was
// waiting, and the edition's own "Approve & Schedule" reads as approving the whole screen.
describe('NewsletterQueue pending-cover legibility', () => {
  const withCover = (id: number, status: 'pending_review' | 'approved'): NewsletterEdition => ({
    ...edition(id, `Edition ${id}`),
    cover_image_url: 'https://cdn.test/c.png',
    cover_image_source: 'ai',
    cover_image_status: status,
  })

  it('flags a pending cover on the queue row, before the edition is opened', async () => {
    serveQueue([withCover(1, 'pending_review'), withCover(2, 'approved')])
    harness(queue())
    await waitFor(() => expect(screen.getByText('Edition 1')).toBeTruthy())
    // One row carries it; the approved edition's row says nothing.
    expect(screen.getAllByText(/Cover needs your approval/)).toHaveLength(1)
  })

  it('says the edition publishes without the cover, and that scheduling is a separate approval', async () => {
    serveQueue([withCover(1, 'pending_review')])
    harness(queue())
    await waitFor(() => expect(screen.getByRole('button', { name: 'Approve cover' })).toBeTruthy())
    expect(screen.getByText(/reaches its slot unapproved/)).toBeTruthy()
    expect(screen.getByText(/this schedules the edition only/)).toBeTruthy()
  })

  it('says none of it once the cover is approved', async () => {
    serveQueue([withCover(1, 'approved')])
    harness(queue())
    await waitFor(() => expect(screen.getByText('PUBLISHES WITH THIS EDITION')).toBeTruthy())
    expect(screen.queryByText(/Cover needs your approval/)).toBeNull()
    expect(screen.queryByText(/this schedules the edition only/)).toBeNull()
  })

  // Issue #1135: "it publishes on time without a cover" is the reassurance that stops an author
  // acting — and for an opted-out draft it is simply false, because the edition waits too.
  it('does not promise an opted-out draft publishes on time without the cover', async () => {
    serveQueue([withCover(1, 'pending_review')], false)
    harness(queue())
    await waitFor(() => expect(screen.getByRole('button', { name: 'Approve cover' })).toBeTruthy())
    expect(screen.queryByText(/reaches its slot unapproved/)).toBeNull()
    expect(screen.getByText(/This edition also waits on your approval/)).toBeTruthy()
  })

  // The queue ROW carries the same claim as the editor, and it is the one an author reads without
  // opening anything — so it has to drop "publishes without it otherwise" for a held draft too.
  it('does not promise the row publishes without the cover for an opted-out draft', async () => {
    serveQueue([withCover(1, 'pending_review')], false)
    harness(queue())
    await waitFor(() => expect(screen.getByText('Edition 1')).toBeTruthy())
    expect(screen.queryByText(/publishes without it otherwise/)).toBeNull()
    expect(screen.getByText(/approve it along with the edition/)).toBeTruthy()
  })

  it('keeps the row wording for an APPROVED edition on an opted-out account', async () => {
    serveQueue([{ ...withCover(1, 'pending_review'), status: 'approved' }], false)
    harness(queue())
    await waitFor(() => expect(screen.getByText('Edition 1')).toBeTruthy())
    expect(screen.getByText(/publishes without it otherwise/)).toBeTruthy()
  })

  it('keeps the original wording for an APPROVED edition on an opted-out account', async () => {
    serveQueue([{ ...withCover(1, 'pending_review'), status: 'approved' }], false)
    harness(queue())
    await waitFor(() => expect(screen.getByRole('button', { name: 'Approve cover' })).toBeTruthy())
    expect(screen.getByText(/reaches its slot unapproved/)).toBeTruthy()
  })
})

// Issue #1806: "Generate with AI" showed a spinner but the cover never updated. The backend
// render can take far longer than a minute — Replicate/FLUX retries once at up to 300s each
// (600s total for one render), and the vision gate can run a full render, including that retry,
// up to twice (IMAGE_GATE_MAX_ATTEMPTS) — a ~1200s worst case. The frontend's poll window has to
// clear THAT, not just the common single-attempt case, or a render that needed a gate repair
// round hits the exact same silent-revert bug the old 12 x 10s = 120s budget did.
describe('NewsletterQueue cover generation polling', () => {
  afterEach(() => vi.useRealTimers())

  it('keeps waiting well past the old two-minute AND the naive six-minute budget, and still catches a cover that lands later', async () => {
    const state: { coverUrl: string | undefined } = { coverUrl: undefined }
    get.mockImplementation(() =>
      Promise.resolve({
        data: {
          detail: {
            editions: [{ ...edition(1, 'First up'), cover_image_url: state.coverUrl }],
            next_publish: null,
            auto_publish_newsletters: true,
          },
        },
      }))
    post.mockResolvedValue({ data: { detail: 'Cover generation started' } })

    harness(queue())
    await waitFor(() => expect(titleBox().value).toBe('First up'))

    vi.useFakeTimers()
    fireEvent.click(screen.getByRole('button', { name: 'Generate with AI' }))
    await act(async () => {})
    expect(screen.getByRole('button', { name: 'Generating…' })).toBeTruthy()

    // Cross well past the old 120s budget, AND past a single gate-repair-round's 600s, with no
    // cover yet — the button must still be waiting (proves the fix covers the documented worst
    // case, not just one doubled Replicate attempt).
    for (let i = 0; i < 90; i++) {
      await act(async () => { await vi.advanceTimersByTimeAsync(10000) })
    }
    expect(screen.getByRole('button', { name: 'Generating…' })).toBeTruthy()

    // The backend finally lands the cover.
    state.coverUrl = 'https://cdn.test/new.png'
    await act(async () => { await vi.advanceTimersByTimeAsync(10000) })

    expect(screen.getByAltText('Newsletter cover').getAttribute('src')).toBe('https://cdn.test/new.png')
    expect(screen.getByRole('button', { name: 'Generate with AI' })).toBeTruthy()
  })

  it('eventually gives up and reverts the button if no cover ever lands', async () => {
    serveQueue([edition(1, 'First up')])
    post.mockResolvedValue({ data: { detail: 'Cover generation started' } })

    harness(queue())
    await waitFor(() => expect(titleBox().value).toBe('First up'))

    vi.useFakeTimers()
    fireEvent.click(screen.getByRole('button', { name: 'Generate with AI' }))
    await act(async () => {})

    for (let i = 0; i < 120; i++) {
      await act(async () => { await vi.advanceTimersByTimeAsync(10000) })
    }

    expect(screen.getByRole('button', { name: 'Generate with AI' })).toBeTruthy()
    expect(screen.getByText(/No cover came back yet/)).toBeTruthy()
  })
})

// Issue #1135: a draft's slot no longer means one universal thing. What happens there depends on
// the account's own `auto_publish_newsletters` setting, so the copy has to report it rather than
// assert it — telling an opted-out user their draft "auto-publishes" is the failure mode.
describe('NewsletterQueue slot copy', () => {
  const serveWithSetting = (autoPublish: boolean, status: NewsletterEdition['status'] = 'draft') =>
    get.mockImplementation(() =>
      Promise.resolve({
        data: {
          detail: {
            editions: [{ ...edition(1, 'First up'), status }],
            next_publish: null,
            auto_publish_newsletters: autoPublish,
          },
        },
      }))

  it('says a draft auto-publishes when the account opted into that', async () => {
    serveWithSetting(true)
    harness(queue())
    await waitFor(() => expect(screen.getByText(/Auto-publishes/)).toBeTruthy())
  })

  it('says a draft waits for approval when the account did not', async () => {
    serveWithSetting(false)
    harness(queue())
    await waitFor(() => expect(screen.getByText(/publishes only once you approve it/)).toBeTruthy())
    expect(screen.queryByText(/Auto-publishes/)).toBeNull()
  })

  it('still says an APPROVED edition publishes at its slot — the toggle only governs drafts', async () => {
    serveWithSetting(false, 'approved')
    harness(queue())
    await waitFor(() => expect(screen.getByText(/Auto-publishes/)).toBeTruthy())
  })
})
