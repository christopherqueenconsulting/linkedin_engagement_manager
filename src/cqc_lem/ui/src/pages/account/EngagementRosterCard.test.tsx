import { describe, expect, it, vi, afterEach, beforeEach } from 'vitest'
import type { ReactNode } from 'react'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import EngagementRosterCard from './EngagementRosterCard'
import type { EngagementTarget } from './types'

const get = vi.fn()
const put = vi.fn()
const del = vi.fn()
vi.mock('../../api/client', () => ({
  default: {
    get: (...args: unknown[]) => get(...args),
    put: (...args: unknown[]) => put(...args),
    delete: (...args: unknown[]) => del(...args),
  },
}))
vi.mock('../../contexts/AuthContext', () => ({ useAuth: () => ({ sessionToken: 'tok' }) }))
vi.mock('../../utils/analytics', () => ({
  maskProps: (className: string) => ({ className }),
  capture: vi.fn(),
  EVENTS: { prefsSaved: 'prefs_saved' },
}))

const TARGETS: EngagementTarget[] = [
  {
    id: 1, profile_url: 'https://www.linkedin.com/in/peer-one', name: 'Peer One',
    category: 'peer', max_comments_per_week: 2, active: true, source: 'user',
  },
]

const routeGet = (targets: EngagementTarget[]) =>
  get.mockResolvedValue({ data: { detail: { targets, suggestions: [] } } })

function harness(ui: ReactNode) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(<QueryClientProvider client={client}>{ui}</QueryClientProvider>)
}

beforeEach(() => {
  get.mockReset()
  put.mockReset()
  del.mockReset()
})
afterEach(cleanup)

describe('EngagementRosterCard — the per-week field says what it controls (issue #956)', () => {
  it('heads the number column with a visible label, not just a "/wk" suffix', async () => {
    routeGet(TARGETS)
    harness(<EngagementRosterCard />)
    await waitFor(() => expect(screen.getByTestId('roster-headers')).toBeTruthy())
    const headers = screen.getByTestId('roster-headers')
    expect(headers.textContent).toContain('Max comments/wk')
    expect(headers.textContent).toContain('Profile URL')
    expect(headers.textContent).toContain('Type')
  })

  it('explains the field on hover, on the input itself', async () => {
    routeGet(TARGETS)
    harness(<EngagementRosterCard />)
    await waitFor(() => expect(screen.getByLabelText('Max comments/wk')).toBeTruthy())
    expect(screen.getByLabelText('Max comments/wk').getAttribute('title'))
      .toBe("LEM will comment on this account's recent posts at most this many times per week (0 pauses the account).")
  })

  // The accessible name and the visible label are the same words on purpose: a screen-reader user and
  // a sighted user must not be told two different things about the same field.
  it('keeps the aria-label in sync with the visible column label', async () => {
    routeGet(TARGETS)
    harness(<EngagementRosterCard />)
    await waitFor(() => expect(screen.getByTestId('roster-headers')).toBeTruthy())
    const field = screen.getByLabelText('Max comments/wk') as HTMLInputElement
    expect(field.type).toBe('number')
    expect(field.value).toBe('2')
  })

  it('tells the reader in prose that 0 pauses an account', async () => {
    routeGet(TARGETS)
    harness(<EngagementRosterCard />)
    await waitFor(() => expect(screen.getByTestId('engagement-roster')).toBeTruthy())
    expect(screen.getByTestId('engagement-roster').textContent)
      .toMatch(/set it to 0 to pause an account without removing it/i)
  })

  it('still saves the edited weekly ceiling', async () => {
    routeGet(TARGETS)
    put.mockResolvedValue({ data: { detail: 'ok' } })
    harness(<EngagementRosterCard />)
    await waitFor(() => expect(screen.getByLabelText('Max comments/wk')).toBeTruthy())
    fireEvent.change(screen.getByLabelText('Max comments/wk'), { target: { value: '0' } })
    fireEvent.click(screen.getByRole('button', { name: /Save Roster/i }))
    await waitFor(() =>
      expect(put).toHaveBeenCalledWith('/user/engagement-targets', {
        session_token: 'tok',
        targets: [{ ...TARGETS[0], max_comments_per_week: 0 }],
      })
    )
  })
})
