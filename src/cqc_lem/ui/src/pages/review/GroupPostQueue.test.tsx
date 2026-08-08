import { describe, expect, it, vi, afterEach, beforeEach } from 'vitest'
import type { ReactNode } from 'react'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import GroupPostQueue, { nextGroupPublishSlot } from './GroupPostQueue'
import type { GroupPostDraft } from '../account/types'

const get = vi.fn()
const put = vi.fn()
vi.mock('../../api/client', () => ({
  default: {
    get: (...args: unknown[]) => get(...args),
    put: (...args: unknown[]) => put(...args),
  },
}))
vi.mock('../../contexts/AuthContext', () => ({ useAuth: () => ({ sessionToken: 'tok' }) }))
vi.mock('../../utils/analytics', () => ({
  maskProps: (className: string) => ({ className }),
  capture: vi.fn(),
  EVENTS: {},
}))

const DRAFT: GroupPostDraft = {
  id: 11,
  group_id: 'g2',
  group_name: 'Sales Pros',
  content: 'A useful insight.',
  status: 'ready',
  created_at: '2026-08-02T15:00:00Z',
}

function harness(ui: ReactNode) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(<QueryClientProvider client={client}>{ui}</QueryClientProvider>)
}

beforeEach(() => {
  get.mockReset()
  put.mockReset()
})
afterEach(cleanup)

describe('GroupPostQueue — publish slot helper', () => {
  it('picks the same Tuesday at 15:00 UTC when before that slot', () => {
    const from = new Date('2026-08-11T10:00:00Z') // Tuesday 10:00 UTC
    const slot = nextGroupPublishSlot(from)
    expect(slot.toISOString()).toBe('2026-08-11T15:00:00.000Z')
  })

  it('picks the following Tuesday when already past the slot', () => {
    const from = new Date('2026-08-11T16:00:00Z') // Tuesday 16:00 UTC
    const slot = nextGroupPublishSlot(from)
    expect(slot.toISOString()).toBe('2026-08-18T15:00:00.000Z')
  })

  it('picks next Tuesday from a Wednesday', () => {
    const from = new Date('2026-08-12T10:00:00Z') // Wednesday
    const slot = nextGroupPublishSlot(from)
    expect(slot.toISOString()).toBe('2026-08-18T15:00:00.000Z')
  })

  it('picks the coming Tuesday from a Sunday', () => {
    const from = new Date('2026-08-09T10:00:00Z') // Sunday
    const slot = nextGroupPublishSlot(from)
    expect(slot.toISOString()).toBe('2026-08-11T15:00:00.000Z')
  })
})

describe('GroupPostQueue — scheduling info', () => {
  it('renders the queued draft, group name, and publish timing', async () => {
    get.mockResolvedValue({ data: { detail: DRAFT } })
    harness(<GroupPostQueue userTimezone="America/New_York" />)

    await waitFor(() => expect(screen.getByText('Sales Pros')).toBeTruthy())
    expect(screen.getByLabelText('Group post text')).toBeTruthy()
    expect((screen.getByLabelText('Group post text') as HTMLTextAreaElement).value).toBe('A useful insight.')
    // Drafted Aug 2 15:00 UTC -> 11:00 AM EDT
    expect(screen.getByText(/Drafted Aug 2, 11:00 AM/i)).toBeTruthy()
    // Next Tuesday 15:00 UTC from Aug 9 (pinned by test helper) -> 11:00 AM EDT
    expect(screen.getByText(/Publishes Aug 11, 11:00 AM/i)).toBeTruthy()
  })

  it('shows an empty-state when no draft is queued', async () => {
    get.mockResolvedValue({ data: { detail: null } })
    harness(<GroupPostQueue userTimezone="America/New_York" />)

    await waitFor(() => expect(screen.getByText('No group post draft queued yet.')).toBeTruthy())
    expect(screen.queryByLabelText('Group post text')).toBeNull()
  })

  it('shows loading state while fetching draft', async () => {
    // Mock a pending request that never resolves
    const pendingPromise = new Promise(() => {})
    get.mockReturnValue(pendingPromise)

    harness(<GroupPostQueue userTimezone="America/New_York" />)

    expect(screen.getByText(/Loading group post draft…/i)).toBeTruthy()

    // Clean up
    get.mockReset()
  })
})

describe('GroupPostQueue — editing', () => {
  it('saves the user rewrite', async () => {
    get.mockResolvedValue({ data: { detail: DRAFT } })
    put.mockResolvedValue({ data: { detail: 'ok' } })
    harness(<GroupPostQueue userTimezone="America/New_York" />)

    await waitFor(() => expect(screen.getByLabelText('Group post text')).toBeTruthy())
    fireEvent.change(screen.getByLabelText('Group post text'), { target: { value: 'My own words.' } })
    fireEvent.click(screen.getByRole('button', { name: /Save post/i }))

    await waitFor(() =>
      expect(put).toHaveBeenCalledWith('/user/group-post-draft', {
        session_token: 'tok',
        content: 'My own words.',
      })
    )
  })

  it('skips this week rather than publishing something unwanted', async () => {
    get.mockResolvedValue({ data: { detail: DRAFT } })
    put.mockResolvedValue({ data: { detail: 'ok' } })
    harness(<GroupPostQueue userTimezone="America/New_York" />)

    await waitFor(() => expect(screen.getByLabelText('Group post text')).toBeTruthy())
    fireEvent.click(screen.getByRole('button', { name: /Skip this week/i }))

    await waitFor(() =>
      expect(put).toHaveBeenCalledWith('/user/group-post-draft', {
        session_token: 'tok',
        status: 'skipped',
      })
    )
  })

  it('disables Save when the text is unchanged', async () => {
    get.mockResolvedValue({ data: { detail: DRAFT } })
    harness(<GroupPostQueue userTimezone="America/New_York" />)

    await waitFor(() => expect(screen.getByLabelText('Group post text')).toBeTruthy())
    expect((screen.getByRole('button', { name: /Save post/i }) as HTMLButtonElement).disabled).toBe(true)
  })

  it('disables Save when the text exceeds the LinkedIn cap', async () => {
    get.mockResolvedValue({ data: { detail: DRAFT } })
    harness(<GroupPostQueue userTimezone="America/New_York" />)

    await waitFor(() => expect(screen.getByLabelText('Group post text')).toBeTruthy())
    fireEvent.change(screen.getByLabelText('Group post text'), { target: { value: 'x'.repeat(3001) } })
    expect((screen.getByRole('button', { name: /Save post/i }) as HTMLButtonElement).disabled).toBe(true)
  })

  it('shows red character count when over limit', async () => {
    get.mockResolvedValue({ data: { detail: DRAFT } })
    harness(<GroupPostQueue userTimezone="America/New_York" />)

    await waitFor(() => expect(screen.getByLabelText('Group post text')).toBeTruthy())
    fireEvent.change(screen.getByLabelText('Group post text'), { target: { value: 'x'.repeat(3001) } })

    const charCount = screen.getByText(/3001\/3000/i)
    expect(charCount.className).toContain('text-red-600')
  })

  it('shows green character count when under limit', async () => {
    get.mockResolvedValue({ data: { detail: DRAFT } })
    harness(<GroupPostQueue userTimezone="America/New_York" />)

    await waitFor(() => expect(screen.getByLabelText('Group post text')).toBeTruthy())
    fireEvent.change(screen.getByLabelText('Group post text'), { target: { value: 'Short text' } })

    const charCount = screen.getByText(/10\/3000/i)
    expect(charCount.className).toContain('text-gray-500')
  })

  it('shows success message after saving', async () => {
    get.mockResolvedValue({ data: { detail: DRAFT } })
    put.mockResolvedValue({ data: { detail: 'ok' } })
    harness(<GroupPostQueue userTimezone="America/New_York" />)

    await waitFor(() => expect(screen.getByLabelText('Group post text')).toBeTruthy())
    fireEvent.change(screen.getByLabelText('Group post text'), { target: { value: 'Updated content' } })
    fireEvent.click(screen.getByRole('button', { name: /Save post/i }))

    await waitFor(() =>
      expect(put).toHaveBeenCalledWith('/user/group-post-draft', {
        session_token: 'tok',
        content: 'Updated content',
      })
    )

    expect(screen.getByText(/Saved\./i)).toBeTruthy()
  })

  it('shows error message when save fails', async () => {
    get.mockResolvedValue({ data: { detail: DRAFT } })
    put.mockRejectedValue(new Error('Network error'))
    harness(<GroupPostQueue userTimezone="America/New_York" />)

    await waitFor(() => expect(screen.getByLabelText('Group post text')).toBeTruthy())
    fireEvent.change(screen.getByLabelText('Group post text'), { target: { value: 'Some content' } })
    fireEvent.click(screen.getByRole('button', { name: /Save post/i }))

    await waitFor(() =>
      expect(put).toHaveBeenCalledWith('/user/group-post-draft', {
        session_token: 'tok',
        content: 'Some content',
      })
    )

    expect(screen.getByText(/Could not save — try again\./i)).toBeTruthy()
  })

  it('shows success message when skipping', async () => {
    get.mockResolvedValue({ data: { detail: DRAFT } })
    put.mockResolvedValue({ data: { detail: 'ok' } })
    harness(<GroupPostQueue userTimezone="America/New_York" />)

    await waitFor(() => expect(screen.getByLabelText('Group post text')).toBeTruthy())
    fireEvent.click(screen.getByRole('button', { name: /Skip this week/i }))

    await waitFor(() =>
      expect(put).toHaveBeenCalledWith('/user/group-post-draft', {
        session_token: 'tok',
        status: 'skipped',
      })
    )

    expect(screen.getByText(/Skipped — no group post this week\./i)).toBeTruthy()
  })
})
