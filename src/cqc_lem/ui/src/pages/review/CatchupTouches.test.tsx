import { describe, expect, it, vi, afterEach, beforeEach } from 'vitest'
import type { ReactNode } from 'react'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import CatchupTouches from './CatchupTouches'

const get = vi.fn()
const put = vi.fn()
vi.mock('../../api/client', () => ({
  default: {
    get: (...args: unknown[]) => get(...args),
    put: (...args: unknown[]) => put(...args),
  },
}))
vi.mock('../../contexts/useAuth', () => ({ useAuth: () => ({ sessionToken: 'tok' }) }))
vi.mock('../../utils/analytics', () => ({
  maskProps: (className: string) => ({ className }),
  capture: vi.fn(),
  EVENTS: {},
}))

// An auto-approve account's touch: it never sits at 'pending', it is drafted 'approved' and the
// drip sends it — the exact row the old 'pending' default hid from this queue (issue #1360).
const SENT_TOUCH = {
  id: 7,
  profile_url: 'https://www.linkedin.com/in/jane',
  person_name: 'Jane Doe',
  event_type: 'new_job',
  event_detail: 'started a new position',
  event_period: '2026-08',
  score: 80,
  message: 'Congratulations on the new role!',
  status: 'sent',
  created_at: '2026-08-02T15:00:00Z',
}

function harness(ui: ReactNode) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(<QueryClientProvider client={client}>{ui}</QueryClientProvider>)
}

const lastGetUrl = () => String(get.mock.calls[get.mock.calls.length - 1][0])

beforeEach(() => {
  get.mockReset()
  put.mockReset()
})
afterEach(cleanup)

describe('CatchupTouches — default view', () => {
  it('asks for every status, so already-sent touches are visible', async () => {
    get.mockResolvedValue({ data: { detail: { touches: [SENT_TOUCH], total: 1 } } })
    harness(<CatchupTouches userTimezone="America/New_York" />)

    await waitFor(() => expect(screen.getByText('Jane Doe')).toBeTruthy())
    expect(lastGetUrl()).not.toContain('status_filter')
    // Two "SENT" nodes: the filter chip and the row's own status badge.
    expect(screen.getAllByText('SENT')).toHaveLength(2)
    // A sent touch is not editable — no approve/cancel controls, just the message that went out.
    expect(screen.getByText('Congratulations on the new role!')).toBeTruthy()
    expect(screen.queryByRole('button', { name: /Approve & send/i })).toBeNull()
  })

  it('puts ALL first in the status filter and selects it', async () => {
    get.mockResolvedValue({ data: { detail: { touches: [], total: 0 } } })
    harness(<CatchupTouches userTimezone="America/New_York" />)

    const chips = ['ALL', 'PENDING', 'APPROVED', 'SENDING', 'SENT', 'SKIPPED', 'FAILED', 'CANCELED']
    const rendered = chips.map((c) => screen.getByRole('button', { name: c }))
    expect(rendered[0].className).toContain('bg-blue-600')
    expect(rendered.slice(1).every((b) => !b.className.includes('bg-blue-600'))).toBe(true)
  })
})

describe('CatchupTouches — truncated page', () => {
  it('says how many touches the page left out', async () => {
    get.mockResolvedValue({ data: { detail: { touches: [SENT_TOUCH], total: 63 } } })
    harness(<CatchupTouches userTimezone="America/New_York" />)

    await waitFor(() => expect(screen.getByText(/62 not listed/)).toBeTruthy())
  })

  it('stays quiet when the page holds every touch', async () => {
    get.mockResolvedValue({ data: { detail: { touches: [SENT_TOUCH], total: 1 } } })
    harness(<CatchupTouches userTimezone="America/New_York" />)

    await waitFor(() => expect(screen.getByText('Jane Doe')).toBeTruthy())
    expect(screen.queryByText(/not listed/)).toBeNull()
  })
})

describe('CatchupTouches — status filter', () => {
  it('scopes the request to the chosen status', async () => {
    get.mockResolvedValue({ data: { detail: { touches: [], total: 0 } } })
    harness(<CatchupTouches userTimezone="America/New_York" />)

    await waitFor(() => expect(get).toHaveBeenCalled())
    fireEvent.click(screen.getByRole('button', { name: 'PENDING' }))

    await waitFor(() => expect(lastGetUrl()).toContain('status_filter=pending'))
  })

  it('offers a way back to ALL when a filtered view is empty', async () => {
    get.mockResolvedValue({ data: { detail: { touches: [], total: 0 } } })
    harness(<CatchupTouches userTimezone="America/New_York" />)

    await waitFor(() => expect(screen.getByText('No catch-up touches yet.')).toBeTruthy())
    expect(screen.queryByRole('button', { name: /Show all statuses/i })).toBeNull()

    fireEvent.click(screen.getByRole('button', { name: 'PENDING' }))
    await waitFor(() => expect(screen.getByText('No catch-up touches with status pending.')).toBeTruthy())

    fireEvent.click(screen.getByRole('button', { name: /Show all statuses/i }))
    await waitFor(() => expect(lastGetUrl()).not.toContain('status_filter'))
  })

})

// Issue #1464: the reporter wanted the queue sorted by date and narrowed to a date range. Both are
// SERVER-side — page 1 of 50 is picked by the sort, so sorting the 50 already in hand would answer
// a different question than the one asked.
describe('CatchupTouches — date sorting', () => {
  it('asks for the highest-scoring page by default', async () => {
    get.mockResolvedValue({ data: { detail: { touches: [], total: 0 } } })
    harness(<CatchupTouches userTimezone="America/New_York" />)

    await waitFor(() => expect(get).toHaveBeenCalled())
    expect(lastGetUrl()).toContain('sort_by=score')
    expect(lastGetUrl()).toContain('sort_order=desc')
  })

  it('re-queries by date when the sort is switched', async () => {
    get.mockResolvedValue({ data: { detail: { touches: [], total: 0 } } })
    harness(<CatchupTouches userTimezone="America/New_York" />)

    await waitFor(() => expect(get).toHaveBeenCalled())
    fireEvent.change(screen.getByLabelText('Sort catch-up touches by'), { target: { value: 'date' } })

    await waitFor(() => expect(lastGetUrl()).toContain('sort_by=date'))
    expect(screen.getByRole('button', { name: /Newest first/i })).toBeTruthy()
  })

  it('flips the direction and says which way it is pointing', async () => {
    get.mockResolvedValue({ data: { detail: { touches: [], total: 0 } } })
    harness(<CatchupTouches userTimezone="America/New_York" />)

    await waitFor(() => expect(get).toHaveBeenCalled())
    fireEvent.change(screen.getByLabelText('Sort catch-up touches by'), { target: { value: 'date' } })
    fireEvent.click(screen.getByRole('button', { name: /Newest first/i }))

    await waitFor(() => expect(lastGetUrl()).toContain('sort_order=asc'))
    expect(screen.getByRole('button', { name: /Oldest first/i })).toBeTruthy()
  })

  it('names the sort in the truncation notice, so 50 of 63 is never read as the top 50 by score', async () => {
    get.mockResolvedValue({ data: { detail: { touches: [SENT_TOUCH], total: 63 } } })
    harness(<CatchupTouches userTimezone="America/New_York" />)

    await waitFor(() => expect(screen.getByText(/highest-scoring of 63/)).toBeTruthy())
    fireEvent.change(screen.getByLabelText('Sort catch-up touches by'), { target: { value: 'date' } })
    await waitFor(() => expect(screen.getByText(/most recent of 63/)).toBeTruthy())
  })
})

describe('CatchupTouches — date range filter', () => {
  it('sends no date bounds until a range is chosen', async () => {
    get.mockResolvedValue({ data: { detail: { touches: [], total: 0 } } })
    harness(<CatchupTouches userTimezone="America/New_York" />)

    await waitFor(() => expect(get).toHaveBeenCalled())
    expect(lastGetUrl()).not.toContain('start_date')
    expect(lastGetUrl()).not.toContain('end_date')
  })

  it('bounds a custom range at the edges of the user timezone day', async () => {
    get.mockResolvedValue({ data: { detail: { touches: [], total: 0 } } })
    harness(<CatchupTouches userTimezone="America/New_York" />)

    await waitFor(() => expect(get).toHaveBeenCalled())
    fireEvent.change(screen.getByLabelText('Date range preset'), { target: { value: 'custom' } })
    fireEvent.change(screen.getByLabelText('Custom start date'), { target: { value: '2026-08-01' } })
    fireEvent.change(screen.getByLabelText('Custom end date'), { target: { value: '2026-08-15' } })

    await waitFor(() => expect(lastGetUrl()).toContain('start_date=2026-08-01T04%3A00%3A00.000Z'))
    expect(lastGetUrl()).toContain('end_date=2026-08-16T03%3A59%3A59.999Z')
  })

  it('never offers a forward-looking preset — every touch was drafted in the past', async () => {
    get.mockResolvedValue({ data: { detail: { touches: [], total: 0 } } })
    harness(<CatchupTouches userTimezone="America/New_York" />)

    await waitFor(() => expect(get).toHaveBeenCalled())
    const options = Array.from(
      (screen.getByLabelText('Date range preset') as HTMLSelectElement).options,
    ).map((o) => o.value)
    expect(options).toContain('last7days')
    expect(options).not.toContain('next30days')
  })

  it('blames the date range for an empty queue and offers a way out', async () => {
    get.mockResolvedValue({ data: { detail: { touches: [], total: 0 } } })
    harness(<CatchupTouches userTimezone="America/New_York" />)

    await waitFor(() => expect(screen.getByText('No catch-up touches yet.')).toBeTruthy())
    fireEvent.change(screen.getByLabelText('Date range preset'), { target: { value: 'today' } })

    await waitFor(() =>
      expect(screen.getByText('No catch-up touches yet in this date range.')).toBeTruthy())
    fireEvent.click(screen.getByRole('button', { name: /Show all dates/i }))
    await waitFor(() => expect(lastGetUrl()).not.toContain('start_date'))
  })
})
