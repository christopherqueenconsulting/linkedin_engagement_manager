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
