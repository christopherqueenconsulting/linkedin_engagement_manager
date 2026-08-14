import { describe, expect, it, vi, afterEach, beforeEach } from 'vitest'
import type { ReactNode } from 'react'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import GroupPostQueue from './GroupPostQueue'
import { nextGroupPublishSlot } from '../../utils/groupPostSlot'
import type { GroupPostDraft } from '../account/types'

const get = vi.fn()
const put = vi.fn()
const post = vi.fn()
vi.mock('../../api/client', () => ({
  default: {
    get: (...args: unknown[]) => get(...args),
    put: (...args: unknown[]) => put(...args),
    post: (...args: unknown[]) => post(...args),
  },
}))
vi.mock('../../contexts/useAuth', () => ({ useAuth: () => ({ sessionToken: 'tok' }) }))
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
  post.mockReset()
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
  // The rendered publish slot is derived from the CURRENT instant, so this assertion is only
  // meaningful against a pinned clock — left on the real one it passes this week and fails next.
  // Only Date is faked: react-query and testing-library's waitFor still need real timers.
  afterEach(() => vi.useRealTimers())

  it('renders the queued draft, group name, and publish timing', async () => {
    vi.useFakeTimers({ toFake: ['Date'] })
    vi.setSystemTime(new Date('2026-08-09T10:00:00Z')) // Sunday
    get.mockResolvedValue({ data: { detail: DRAFT } })
    harness(<GroupPostQueue userTimezone="America/New_York" />)

    await waitFor(() => expect(screen.getByText('Sales Pros')).toBeTruthy())
    expect(screen.getByLabelText('Group post text')).toBeTruthy()
    expect((screen.getByLabelText('Group post text') as HTMLTextAreaElement).value).toBe('A useful insight.')
    // Drafted Aug 2 15:00 UTC -> 11:00 AM EDT
    expect(screen.getByText(/Drafted Aug 2, 11:00 AM/i)).toBeTruthy()
    // Next Tuesday 15:00 UTC from the pinned Sunday -> 11:00 AM EDT
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

  it('keeps the skip confirmation once the retired draft leaves the queue', async () => {
    // A skipped draft the server has since replaced (or a read that comes back empty) unmounts the
    // panel holding the button, so the confirmation has to outlive it or the click reads as having
    // done nothing.
    get.mockResolvedValueOnce({ data: { detail: DRAFT } }).mockResolvedValue({ data: { detail: null } })
    put.mockResolvedValue({ data: { detail: 'ok' } })
    harness(<GroupPostQueue userTimezone="America/New_York" />)

    await waitFor(() => expect(screen.getByLabelText('Group post text')).toBeTruthy())
    fireEvent.click(screen.getByRole('button', { name: /Skip this week/i }))

    await waitFor(() => expect(screen.getByText('No group post draft queued yet.')).toBeTruthy())
    expect(screen.getByText(/Skipped — no group post this week\./i)).toBeTruthy()
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

describe('GroupPostQueue — statuses (issue #1224)', () => {
  const SKIPPED: GroupPostDraft = { ...DRAFT, status: 'skipped' }

  it('shows a skipped draft with the way back into the queue', async () => {
    get.mockResolvedValue({ data: { detail: SKIPPED } })
    put.mockResolvedValue({ data: { detail: 'ok' } })
    harness(<GroupPostQueue userTimezone="America/New_York" />)

    await waitFor(() => expect(screen.getByText('SKIPPED')).toBeTruthy())
    expect(screen.queryByRole('button', { name: /Skip this week/i })).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: /Undo skip/i }))

    await waitFor(() =>
      expect(put).toHaveBeenCalledWith('/user/group-post-draft', {
        session_token: 'tok',
        status: 'ready',
      })
    )
    expect(screen.getByText(/Skip undone — back in the queue for this week\./i)).toBeTruthy()
  })

  it('a queued draft offers Skip, not Restore', async () => {
    get.mockResolvedValue({ data: { detail: DRAFT } })
    harness(<GroupPostQueue userTimezone="America/New_York" />)

    await waitFor(() => expect(screen.getByText('READY')).toBeTruthy())
    expect(screen.queryByRole('button', { name: /Undo skip/i })).toBeNull()
  })

  it('drops the undo control once the publish slot has passed (issue #1415)', async () => {
    // The server refuses the restore from here on, so offering the control would be a button that
    // only ever produces an error.
    get.mockResolvedValue({ data: { detail: { ...SKIPPED, can_undo_skip: false } } })
    harness(<GroupPostQueue userTimezone="America/New_York" />)

    await waitFor(() => expect(screen.getByText('SKIPPED')).toBeTruthy())
    expect(screen.queryByRole('button', { name: /Undo skip/i })).toBeNull()
    expect(screen.getAllByText(/the skip is final|can no longer be put back/i).length)
      .toBeGreaterThan(0)
  })

  it('keeps the undo control while the window is open (issue #1415)', async () => {
    get.mockResolvedValue({ data: { detail: { ...SKIPPED, can_undo_skip: true } } })
    harness(<GroupPostQueue userTimezone="America/New_York" />)

    await waitFor(() => expect(screen.getByText('SKIPPED')).toBeTruthy())
    expect(screen.getByRole('button', { name: /Undo skip/i })).toBeTruthy()
  })
})

describe('GroupPostQueue — media (issue #1224)', () => {
  it('renders the best-practice list the API served with the draft', async () => {
    get.mockResolvedValue({
      data: { detail: { ...DRAFT, best_practices: ['Open with a question.', 'Stay native.'] } },
    })
    harness(<GroupPostQueue userTimezone="America/New_York" />)

    await waitFor(() => expect(screen.getByText('Open with a question.')).toBeTruthy())
    expect(screen.getByText('Stay native.')).toBeTruthy()
  })

  it('uploads a file then attaches the URL the server issued', async () => {
    get.mockResolvedValue({ data: { detail: DRAFT } })
    post.mockResolvedValue({ data: { detail: { image_url: 'http://api/assets?file_name=a.png' } } })
    put.mockResolvedValue({ data: { detail: 'ok' } })
    harness(<GroupPostQueue userTimezone="America/New_York" />)

    await waitFor(() => expect(screen.getByLabelText('Group post media file')).toBeTruthy())
    const file = new File(['x'], 'a.png', { type: 'image/png' })
    fireEvent.change(screen.getByLabelText('Group post media file'), { target: { files: [file] } })

    await waitFor(() => expect(post).toHaveBeenCalledWith('/user/post/image', expect.any(FormData)))
    await waitFor(() =>
      expect(put).toHaveBeenCalledWith('/user/group-post-draft', {
        session_token: 'tok',
        media_url: 'http://api/assets?file_name=a.png',
      })
    )
  })

  it('generates an image from the text on screen, not the stale saved copy', async () => {
    get.mockResolvedValue({ data: { detail: DRAFT } })
    post.mockResolvedValue({ data: { detail: { image_url: 'http://api/assets?file_name=b.png' } } })
    put.mockResolvedValue({ data: { detail: 'ok' } })
    harness(<GroupPostQueue userTimezone="America/New_York" />)

    await waitFor(() => expect(screen.getByLabelText('Group post text')).toBeTruthy())
    fireEvent.change(screen.getByLabelText('Group post text'), { target: { value: 'Edited text.' } })
    fireEvent.click(screen.getByRole('button', { name: /Generate with AI/i }))

    await waitFor(() =>
      expect(post).toHaveBeenCalledWith('/user/post/image/generate', {
        session_token: 'tok',
        content: 'Edited text.',
      })
    )
  })

  it('shows the attached image and lets it be removed', async () => {
    get.mockResolvedValue({
      data: { detail: { ...DRAFT, media_url: 'http://api/assets?file_name=a.png', media_type: 'image' } },
    })
    put.mockResolvedValue({ data: { detail: 'ok' } })
    harness(<GroupPostQueue userTimezone="America/New_York" />)

    await waitFor(() => expect(screen.getByAltText('Group post media')).toBeTruthy())
    fireEvent.click(screen.getByRole('button', { name: /Remove media/i }))

    await waitFor(() =>
      expect(put).toHaveBeenCalledWith('/user/group-post-draft', {
        session_token: 'tok',
        remove_media: true,
      })
    )
  })

  it('refuses to generate an image with nothing to draw from', async () => {
    get.mockResolvedValue({ data: { detail: { ...DRAFT, content: '' } } })
    harness(<GroupPostQueue userTimezone="America/New_York" />)

    await waitFor(() => expect(screen.getByRole('button', { name: /Generate with AI/i })).toBeTruthy())
    fireEvent.click(screen.getByRole('button', { name: /Generate with AI/i }))

    expect(post).not.toHaveBeenCalled()
    expect(screen.getByText(/Write the post first/i)).toBeTruthy()
  })
})
