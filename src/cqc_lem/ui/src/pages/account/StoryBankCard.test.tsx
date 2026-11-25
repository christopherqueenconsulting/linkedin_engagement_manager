import { describe, expect, it, vi, afterEach } from 'vitest'
import type { ReactNode } from 'react'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import StoryBankCard from './StoryBankCard'
import type { StoryEntry } from './types'

const get = vi.fn()
const put = vi.fn()
const post = vi.fn()
const del = vi.fn()
vi.mock('../../api/client', () => ({
  default: {
    get: (...args: unknown[]) => get(...args),
    put: (...args: unknown[]) => put(...args),
    post: (...args: unknown[]) => post(...args),
    delete: (...args: unknown[]) => del(...args),
  },
}))
let isAdmin = false
vi.mock('../../contexts/useAuth', () => ({ useAuth: () => ({ sessionToken: 'tok', isAdmin }) }))
vi.mock('../../utils/analytics', () => ({
  EVENTS: { storyBankSaved: 'story_bank_saved' },
  capture: vi.fn(),
  maskProps: (className: string) => ({ className }),
}))

const serveEntries = (entries: StoryEntry[]) =>
  get.mockImplementation(() =>
    Promise.resolve({ data: { detail: { entries, kinds: [], target_entries: 5 } } }))

function harness(ui: ReactNode) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return { client, ...render(<QueryClientProvider client={client}>{ui}</QueryClientProvider>) }
}

const bodyBox = () => screen.getByLabelText('What actually happened') as HTMLTextAreaElement

afterEach(() => {
  cleanup(); get.mockReset(); put.mockReset(); post.mockReset(); del.mockReset(); isAdmin = false
})

describe('StoryBankCard call ingest (issue #2629, owner-only)', () => {
  it('hides the call-ingest panel from a non-owner account', async () => {
    serveEntries([])
    harness(<StoryBankCard />)
    await waitFor(() => expect(screen.getByTestId('story-bank')).toBeTruthy())
    expect(screen.queryByTestId('call-ingest')).toBeNull()
  })

  it('shows the call-ingest panel to an admin account', async () => {
    isAdmin = true
    serveEntries([])
    harness(<StoryBankCard />)
    await waitFor(() => expect(screen.getByTestId('call-ingest')).toBeTruthy())
  })
})

describe('StoryBankCard save', () => {
  // React Query keeps serving the row it already has while a refetch is in flight, so dropping the
  // local draft the moment the PUT resolves would make a just-saved entry vanish for the length of
  // that round trip — and stay gone if it never answers.
  it('keeps a just-saved entry on screen until the refetch answers', async () => {
    serveEntries([])
    harness(<StoryBankCard />)
    await waitFor(() => expect(screen.getByTestId('story-bank')).toBeTruthy())

    fireEvent.click(screen.getByText('+ Add entry'))
    fireEvent.change(bodyBox(), { target: { value: 'Cut their reporting run from 6h to 9min.' } })

    // The refetch this save triggers is held open, so the assertion below is made in exactly the
    // window the bug lived in.
    let answer: (rows: StoryEntry[]) => void = () => {}
    const held = new Promise<StoryEntry[]>((resolve) => { answer = resolve })
    get.mockImplementation(() =>
      held.then((entries) => ({ data: { detail: { entries, kinds: [], target_entries: 5 } } })))
    put.mockResolvedValue({ data: { detail: {} } })

    fireEvent.click(screen.getByText('Save Story Bank'))
    await waitFor(() => expect(put).toHaveBeenCalled())
    await waitFor(() => expect(get.mock.calls.length).toBeGreaterThan(1))

    expect(bodyBox().value).toBe('Cut their reporting run from 6h to 9min.')

    answer([{ id: 7, kind: 'client_win', title: null, body: 'Cut their reporting run from 6h to 9min.', happened_at: null, active: true }])
    await waitFor(() => expect(screen.getByText('Story bank saved.')).toBeTruthy())
    expect(bodyBox().value).toBe('Cut their reporting run from 6h to 9min.')
  })
})

describe('StoryBankCard agent drafts (issue #2804)', () => {
  const entry = (over: Partial<StoryEntry>): StoryEntry => ({
    id: 7, kind: 'client_win', title: 'Saved a launch', body: 'We shipped on time for Acme.',
    happened_at: null, active: true, verified: true, source: null, ...over,
  })
  const BADGE = 'Added by your agent — not yet confirmed'

  it('badges an unconfirmed agent entry with Confirm and Discard', async () => {
    serveEntries([entry({ verified: false, source: 'mcp' })])
    harness(<StoryBankCard />)
    await waitFor(() => expect(screen.getByText(BADGE)).toBeTruthy())
    expect(screen.getByRole('button', { name: 'Confirm' })).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Discard' })).toBeTruthy()
  })

  it("shows no badge on the user's own or an already-confirmed entry", async () => {
    serveEntries([entry({ id: 1 }), entry({ id: 2, verified: true, source: 'mcp' })])
    harness(<StoryBankCard />)
    await waitFor(() => expect(screen.getAllByLabelText('What actually happened')).toHaveLength(2))
    expect(screen.queryByText(BADGE)).toBeNull()
  })

  it("does not count an unconfirmed entry as one of the user's entries", async () => {
    serveEntries([entry({ id: 1 }), entry({ id: 2, verified: false, source: 'mcp' })])
    harness(<StoryBankCard />)
    await waitFor(() => expect(screen.getByTestId('story-bank-progress').textContent)
      .toContain('1 of 5 entries'))
  })

  it('Confirm posts the entry id and refetches', async () => {
    serveEntries([entry({ verified: false, source: 'mcp' })])
    post.mockResolvedValue({ data: { detail: 'ok' } })
    harness(<StoryBankCard />)
    await waitFor(() => expect(screen.getByText(BADGE)).toBeTruthy())
    const before = get.mock.calls.length
    fireEvent.click(screen.getByRole('button', { name: 'Confirm' }))
    await waitFor(() => expect(post).toHaveBeenCalledWith('/user/story-bank/confirm',
      { session_token: 'tok', entry_id: 7 }))
    await waitFor(() => expect(get.mock.calls.length).toBeGreaterThan(before))
  })

  it('Discard deletes the entry', async () => {
    serveEntries([entry({ verified: false, source: 'mcp' })])
    del.mockResolvedValue({ data: {} })
    harness(<StoryBankCard />)
    await waitFor(() => expect(screen.getByText(BADGE)).toBeTruthy())
    fireEvent.click(screen.getByRole('button', { name: 'Discard' }))
    await waitFor(() => expect(del).toHaveBeenCalledWith('/user/story-bank',
      { data: { session_token: 'tok', entry_id: 7 } }))
    expect(screen.queryByText(BADGE)).toBeNull()
  })

  it('names the entry as an agent draft in the Agents activity feed', async () => {
    const { describe: describeEvent } = await import('../agents/agentActivity')
    expect(describeEvent({ event: 'mcp_story_draft', details: { tool: 'lem_add_story_draft', target_id: 3 } } as never))
      .toContain('story-bank draft')
  })
})
