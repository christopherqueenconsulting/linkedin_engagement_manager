import { describe, expect, it, vi, afterEach, beforeEach } from 'vitest'
import type { ReactNode } from 'react'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import EngagementRosterCard from './EngagementRosterCard'
import { EngagementPrefsProvider } from './settings/EngagementPrefsContext'
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

// The auto-follow toggle lives on this card but is stored in the shared engagement-preferences row,
// so the card is mounted inside the same provider the settings hub gives it.
const PREFS = {
  has_saved_preferences: true, roster_auto_follow: false, max_follows_per_day: 3,
  max_comments_per_day: 20, max_dms_per_day: 20, max_invites_per_day: 10,
  max_catchup_touches_per_day: 5, max_catchup_touches_allowed: 5,
}

const routeGet = (targets: EngagementTarget[], prefs: Record<string, unknown> = {}) =>
  get.mockImplementation((url: string) =>
    Promise.resolve(
      String(url).includes('engagement-preferences')
        ? { data: { detail: { ...PREFS, ...prefs } } }
        : { data: { detail: { targets, suggestions: [] } } }
    )
  )

function harness(ui: ReactNode) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <EngagementPrefsProvider>{ui}</EngagementPrefsProvider>
    </QueryClientProvider>
  )
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

describe("EngagementRosterCard — targets LEM can't comment on (issue #962)", () => {
  const blocked = (streak: number, extra: Partial<EngagementTarget> = {}): EngagementTarget[] => [
    { ...TARGETS[0], comment_blocked_streak: streak, ...extra },
  ]

  it('badges a target that has been un-commentable for two visits', async () => {
    routeGet(blocked(2))
    harness(<EngagementRosterCard />)
    await waitFor(() => expect(screen.getByTestId('roster-blocked-badge')).toBeTruthy())
    const badge = screen.getByTestId('roster-blocked-badge')
    expect(badge.textContent).toContain("Can't comment")
    expect(badge.textContent).toMatch(/follow or connect/i)
    // The help text has to say WHY, not just that something is wrong.
    expect(badge.getAttribute('title')).toMatch(/connections or followers/i)
  })

  it('says nothing after a single blocked visit', async () => {
    // One page that happened to render only reshares is not evidence the author blocks comments.
    routeGet(blocked(1))
    harness(<EngagementRosterCard />)
    await waitFor(() => expect(screen.getByTestId('engagement-roster')).toBeTruthy())
    expect(screen.queryByTestId('roster-blocked-badge')).toBeNull()
  })

  it('badges a target whose follow attempts were exhausted', async () => {
    routeGet(blocked(0, { follow_status: 'follow_failed' }))
    harness(<EngagementRosterCard />)
    await waitFor(() => expect(screen.getByTestId('roster-follow-failed-badge')).toBeTruthy())
    expect(screen.getByTestId('roster-follow-failed-badge').getAttribute('title'))
      .toMatch(/will not keep retrying/i)
  })

  it('shows a plain Following marker once a target is followed', async () => {
    routeGet(blocked(0, { follow_status: 'following' }))
    harness(<EngagementRosterCard />)
    await waitFor(() => expect(screen.getByTestId('roster-following-badge')).toBeTruthy())
    expect(screen.queryByTestId('roster-follow-failed-badge')).toBeNull()
  })

  it('leaves a healthy target unbadged', async () => {
    routeGet(TARGETS)
    harness(<EngagementRosterCard />)
    await waitFor(() => expect(screen.getByTestId('engagement-roster')).toBeTruthy())
    expect(screen.queryByTestId('roster-blocked-badge')).toBeNull()
    expect(screen.queryByTestId('roster-following-badge')).toBeNull()
  })
})

describe('EngagementRosterCard — opt-in auto-follow toggle (issue #962)', () => {
  it('renders the toggle off by default and names the daily ceiling', async () => {
    routeGet(TARGETS)
    harness(<EngagementRosterCard />)
    await waitFor(() => expect(screen.getByTestId('roster-auto-follow')).toBeTruthy())
    const toggle = screen.getByTestId('roster-auto-follow') as HTMLInputElement
    expect(toggle.checked).toBe(false)
    expect(screen.getByTestId('engagement-roster').textContent).toContain('3')
    expect(screen.getByTestId('engagement-roster').textContent)
      .toMatch(/connections-only one still needs a connection request/i)
  })

  it('reflects a saved opt-in', async () => {
    routeGet(TARGETS, { roster_auto_follow: true })
    harness(<EngagementRosterCard />)
    await waitFor(() => expect(screen.getByTestId('roster-auto-follow')).toBeTruthy())
    expect((screen.getByTestId('roster-auto-follow') as HTMLInputElement).checked).toBe(true)
  })

  it('saves the toggle with the roster — a switch that needs a second save button elsewhere reads as broken', async () => {
    routeGet(TARGETS)
    put.mockResolvedValue({ data: { detail: 'ok' } })
    harness(<EngagementRosterCard />)
    await waitFor(() => expect(screen.getByTestId('roster-auto-follow')).toBeTruthy())
    fireEvent.click(screen.getByTestId('roster-auto-follow'))
    fireEvent.click(screen.getByRole('button', { name: /Save Roster/i }))
    await waitFor(() =>
      expect(put.mock.calls.some(
        ([url, body]) => url === '/user/engagement-preferences'
          && (body as { roster_auto_follow?: boolean }).roster_auto_follow === true,
      )).toBe(true)
    )
  })

  it('does not touch the preferences row when the toggle was not moved', async () => {
    routeGet(TARGETS)
    put.mockResolvedValue({ data: { detail: 'ok' } })
    harness(<EngagementRosterCard />)
    await waitFor(() => expect(screen.getByLabelText('Max comments/wk')).toBeTruthy())
    fireEvent.change(screen.getByLabelText('Max comments/wk'), { target: { value: '1' } })
    fireEvent.click(screen.getByRole('button', { name: /Save Roster/i }))
    await waitFor(() => expect(put).toHaveBeenCalledWith('/user/engagement-targets', expect.anything()))
    expect(put.mock.calls.every(([url]) => url === '/user/engagement-targets')).toBe(true)
  })
})
