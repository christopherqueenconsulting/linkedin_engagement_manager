import { describe, expect, it, vi, afterEach, beforeEach } from 'vitest'
import type { ReactNode } from 'react'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import ProfileAuditCard from './ProfileAuditCard'

const get = vi.fn()
const post = vi.fn()
vi.mock('../../api/client', () => ({
  default: { get: (...a: unknown[]) => get(...a), post: (...a: unknown[]) => post(...a) },
}))
vi.mock('../../contexts/useAuth', () => ({ useAuth: () => ({ sessionToken: 'tok' }) }))

const finding = (over: Record<string, unknown>) => ({
  check_id: 'custom_url', status: 'fail', weight: 10, detail: 'URL is auto-generated.',
  fix_hint: 'Claim a custom URL.', state: 'open', delta: null, ...over,
})

function payload(over: Record<string, unknown> = {}) {
  return {
    data: {
      detail: {
        latest: {
          audit_id: 2, period_key: '2026-W47', kind: 'weekly', score: 70, rubric_version: '1',
          created_at: '2026-11-17T12:00:00', open_count: 2,
          findings: [
            finding({}),
            finding({ check_id: 'headline_outcome', status: 'warn', detail: 'No outcome.' }),
            finding({ check_id: 'banner_custom', status: 'pass', state: 'passing' }),
          ],
        },
        trend: [
          { period_key: '2026-W46', kind: 'weekly', score: 50, opened_count: 0, closed_count: 0, created_at: null },
          { period_key: '2026-W47', kind: 'weekly', score: 70, opened_count: 0, closed_count: 1, created_at: null },
        ],
        correlation: {
          note: 'Shown side by side. Not claimed to be cause.',
          rows: [
            { period_key: '2026-W46', score: 50, open_count: 3, follower_count: 100, profile_views: null, follower_change: null },
            { period_key: '2026-W47', score: 70, open_count: 2, follower_count: 130, profile_views: 40, follower_change: 30 },
          ],
        },
        ...over,
      },
    },
  }
}

function harness(ui: ReactNode) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(<QueryClientProvider client={client}>{ui}</QueryClientProvider>)
}

beforeEach(() => {
  get.mockReset()
  post.mockReset()
})
afterEach(cleanup)

describe('ProfileAuditCard (issue #2753)', () => {
  it('shows the score trend, open findings and the audience join without a causal claim', async () => {
    get.mockResolvedValue(payload())
    harness(<ProfileAuditCard />)
    await waitFor(() => expect(screen.getAllByTestId('audit-finding')).toHaveLength(3))
    expect(screen.getAllByTestId('audit-trend-point')).toHaveLength(2)
    expect(screen.getAllByText('70/100').length).toBeGreaterThan(0)
    expect(screen.getByTestId('audit-open-count').textContent).toBe('2 open findings')
    expect(get).toHaveBeenCalledWith('/user/profile-audit?session_token=tok')
    const rows = screen.getAllByTestId('audit-correlation-row')
    expect(rows[1].textContent).toContain('+30')
    // An unread reading is named, never shown as 0.
    expect(rows[0].textContent).toContain('Not measured')
    expect(screen.getByText(/Not claimed to be cause/)).toBeTruthy()
  })

  it('marks a finding done through the API and refreshes', async () => {
    get.mockResolvedValue(payload())
    post.mockResolvedValue({ data: { detail: 'ok' } })
    harness(<ProfileAuditCard />)
    await waitFor(() => expect(screen.getByRole('button', { name: 'Mark done: custom url' })).toBeTruthy())
    fireEvent.click(screen.getByRole('button', { name: 'Mark done: custom url' }))
    await waitFor(() =>
      expect(post).toHaveBeenCalledWith('/user/profile-audit/finding',
        { session_token: 'tok', check_id: 'custom_url', action: 'done' }))
    await waitFor(() => expect(get).toHaveBeenCalledTimes(2))
  })

  it('ignores a finding through the API', async () => {
    get.mockResolvedValue(payload())
    post.mockResolvedValue({ data: { detail: 'ok' } })
    harness(<ProfileAuditCard />)
    await waitFor(() => expect(screen.getByRole('button', { name: 'Ignore: headline outcome' })).toBeTruthy())
    fireEvent.click(screen.getByRole('button', { name: 'Ignore: headline outcome' }))
    await waitFor(() =>
      expect(post).toHaveBeenCalledWith('/user/profile-audit/finding',
        { session_token: 'tok', check_id: 'headline_outcome', action: 'ignored' }))
  })

  it('offers reopen for an ignored finding and no actions for a passing one', async () => {
    const detail = payload().data.detail
    detail.latest.findings = [finding({ state: 'ignored' }), finding({ check_id: 'banner_custom', status: 'pass', state: 'passing' })]
    get.mockResolvedValue({ data: { detail } })
    harness(<ProfileAuditCard />)
    await waitFor(() => expect(screen.getByRole('button', { name: 'Reopen: custom url' })).toBeTruthy())
    expect(screen.queryByRole('button', { name: /^Mark done/ })).toBeNull()
    expect(screen.getAllByRole('button')).toHaveLength(1)
  })

  it('says so when a save fails', async () => {
    get.mockResolvedValue(payload())
    post.mockRejectedValue(new Error('503'))
    harness(<ProfileAuditCard />)
    await waitFor(() => expect(screen.getByRole('button', { name: 'Mark done: custom url' })).toBeTruthy())
    fireEvent.click(screen.getByRole('button', { name: 'Mark done: custom url' }))
    await waitFor(() => expect(screen.getByRole('alert').textContent).toMatch(/Could not save/))
  })

  it('reports unavailable, not empty, when the read fails', async () => {
    get.mockRejectedValue(new Error('503'))
    harness(<ProfileAuditCard />)
    await waitFor(() => expect(screen.getByText(/temporarily unavailable/i)).toBeTruthy())
    expect(screen.queryByText(/No audit yet/i)).toBeNull()
  })

  it('shows an empty state before the first audit', async () => {
    get.mockResolvedValue(payload({ latest: null, trend: [], correlation: { note: 'n', rows: [] } }))
    harness(<ProfileAuditCard />)
    await waitFor(() => expect(screen.getByText(/No audit yet/i)).toBeTruthy())
  })

  it('renders nothing while the flag is off (404)', async () => {
    const err = Object.assign(new Error('404'), { isAxiosError: true, response: { status: 404 } })
    get.mockRejectedValue(err)
    harness(<ProfileAuditCard />)
    await waitFor(() => expect(get).toHaveBeenCalled())
    await new Promise((r) => setTimeout(r, 0))
    expect(screen.queryByTestId('profile-audit')).toBeNull()
  })
})
