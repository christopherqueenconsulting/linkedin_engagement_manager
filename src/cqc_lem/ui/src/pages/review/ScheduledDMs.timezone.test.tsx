import { describe, expect, it, vi, afterEach, beforeEach } from 'vitest'
import type { ReactNode } from 'react'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import ScheduledDMs from './ScheduledDMs'

// Issue #774: the DM picker is a bare wall clock too, and its send time is stored the same way a
// post's is. Until /user/timezone answers the zone on offer is the BROWSER's, so a complete draft
// must not be sendable — otherwise the DM goes out offset by the difference between the two zones.
const get = vi.fn()
const post = vi.fn()

vi.mock('../../api/client', () => ({
  default: {
    get: (...args: unknown[]) => get(...args),
    post: (...args: unknown[]) => post(...args),
    put: vi.fn(),
  },
}))

vi.mock('../../contexts/AuthContext', () => ({
  useAuth: () => ({ user: { email: 'test@example.com', userId: 1 }, sessionToken: 'tok' }),
}))

vi.mock('../../utils/analytics', () => ({
  maskProps: (className: string) => ({ className }),
}))

function harness(ui: ReactNode) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(<QueryClientProvider client={client}>{ui}</QueryClientProvider>)
}

beforeEach(() => {
  get.mockReset()
  post.mockReset()
  get.mockResolvedValue({ data: { detail: { dms: [], total: 0 } } })
  post.mockResolvedValue({ data: { detail: {} } })
})

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

function fillDraft(container: HTMLElement) {
  fireEvent.change(screen.getByPlaceholderText(/Recipient profile URL/), {
    target: { value: 'https://www.linkedin.com/in/someone' },
  })
  fireEvent.change(screen.getByPlaceholderText(/Your message/), { target: { value: 'Hi there' } })
  fireEvent.change(container.querySelector('input[type="datetime-local"]') as HTMLInputElement, {
    target: { value: '2026-07-28T09:00' },
  })
}

const scheduleButton = () => screen.getByRole('button', { name: 'Approve & schedule' }) as HTMLButtonElement
const draftButton = () => screen.getByRole('button', { name: 'Save draft' }) as HTMLButtonElement

describe('ScheduledDMs with an unresolved timezone (issue #774)', () => {
  it('will not send an otherwise-complete draft, and says the zone is still loading', () => {
    const { container } = harness(<ScheduledDMs userTimezone="UTC" timezoneResolved={false} />)
    fillDraft(container)

    expect(draftButton().disabled).toBe(true)
    expect(scheduleButton().disabled).toBe(true)
    fireEvent.click(scheduleButton())
    expect(post).not.toHaveBeenCalled()

    expect(screen.getByText(/loading your timezone/)).toBeDefined()
    expect(screen.queryByText('UTC')).toBeNull()
  })

  it('sends the same draft once the zone is the user’s own', async () => {
    const { container } = harness(<ScheduledDMs userTimezone="America/New_York" timezoneResolved />)
    fillDraft(container)

    expect(scheduleButton().disabled).toBe(false)
    fireEvent.click(scheduleButton())

    // 09:00 in New York on that date is 13:00Z — the conversion the unresolved case must not guess.
    await waitFor(() =>
      expect(post).toHaveBeenCalledWith(
        '/schedule_dm',
        expect.objectContaining({ scheduled_datetime: '2026-07-28T13:00:00.000Z' }),
      ),
    )
  })
})
