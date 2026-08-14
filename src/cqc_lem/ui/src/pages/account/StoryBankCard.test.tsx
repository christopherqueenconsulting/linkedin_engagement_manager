import { describe, expect, it, vi, afterEach } from 'vitest'
import type { ReactNode } from 'react'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import StoryBankCard from './StoryBankCard'
import type { StoryEntry } from './types'

const get = vi.fn()
const put = vi.fn()
vi.mock('../../api/client', () => ({
  default: {
    get: (...args: unknown[]) => get(...args),
    put: (...args: unknown[]) => put(...args),
    delete: vi.fn(),
  },
}))
vi.mock('../../contexts/useAuth', () => ({ useAuth: () => ({ sessionToken: 'tok' }) }))
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

afterEach(() => { cleanup(); get.mockReset(); put.mockReset() })

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
