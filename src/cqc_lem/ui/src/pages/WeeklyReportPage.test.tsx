import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import WeeklyReportPage from './WeeklyReportPage'

const get = vi.fn()
vi.mock('../api/client', () => ({ default: { get: (...args: unknown[]) => get(...args) } }))
vi.mock('../contexts/useAuth', () => ({ useAuth: () => ({ sessionToken: 'cookie' }) }))

const flagState = vi.hoisted(() => ({ on: true }))
vi.mock('../hooks/useFeatureFlags', () => ({
  FLAGS: { weeklyReport: 'weekly-report-enabled' },
  useFeatureFlags: () => ({ isLoading: false, data: { flags: { 'weekly-report-enabled': flagState.on } } }),
}))

const EMPTY_REPORT = {
  week_start: '2026-11-17T00:00:00Z',
  week_end: '2026-11-24T00:00:00Z',
  numbers: { posts_published: null, median_impressions: null, organic_engagement_rate: null, follower_delta: null },
  deltas: {},
  icp_share: null,
  comment_volume: { automated_total: null, assist_total: null },
  actions: [],
  not_measured: [{ key: 'posts_published', label: 'posts published (could not read posts)' }],
}

function mount() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={['/report']}>
        <Routes>
          <Route path="/report" element={<WeeklyReportPage />} />
          <Route path="/" element={<div>home</div>} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

function answer(report: unknown, icp?: unknown) {
  get.mockImplementation((url: string) => {
    if (url.startsWith('/user/weekly-report')) return Promise.resolve({ data: { detail: report } })
    if (url.startsWith('/user/icp-audience-report')) return Promise.resolve({ data: { detail: icp } })
    return Promise.reject(new Error(url))
  })
}

describe('WeeklyReportPage', () => {
  beforeEach(() => { flagState.on = true; get.mockReset() })
  afterEach(cleanup)

  it('flag off sends the visitor home without reading the report', () => {
    flagState.on = false
    mount()
    expect(screen.getByText('home')).toBeTruthy()
    expect(get).not.toHaveBeenCalled()
  })

  it('renders with every metric missing: could-not-measure line and no zeros', async () => {
    answer(EMPTY_REPORT)
    mount()
    expect(await screen.findByRole('status')).toHaveProperty('textContent', expect.stringContaining('Could not measure'))
    expect(screen.getAllByText('Could not measure').length).toBeGreaterThanOrEqual(7)
    expect(screen.queryByText('0')).toBeNull()
    expect(screen.getByText('posts published (could not read posts)')).toBeTruthy()
  })

  it('shows a real zero as 0, distinct from unmeasured', async () => {
    answer({ ...EMPTY_REPORT, numbers: { ...EMPTY_REPORT.numbers, posts_published: 0 } })
    mount()
    expect(await screen.findByText('0')).toBeTruthy()
    expect(screen.queryByRole('status')).toBeNull()
  })

  it('a failed read says so rather than rendering an empty report', async () => {
    get.mockRejectedValue(new Error('boom'))
    mount()
    expect(await screen.findByRole('alert')).toBeTruthy()
  })

  it('reads the ICP report only on demand and shows n and the insufficient floor', async () => {
    answer(EMPTY_REPORT, {
      readable: true, min_n: 5,
      overall: { engagers: 9, n: 3, inside: 1, share: null, insufficient: true },
      segments: [{ segment_id: 'cto', name: 'CTOs', n: 2, share_of_segmented: null, insufficient: true }],
      gap_by_month: [{ month: '2026-11', n: 3, share: null, gap: null, insufficient: true }],
      not_measured: [{ key: 'titles', label: 'engager job titles (not stored for engagers)' }],
    })
    mount()
    const run = await screen.findByText('Run the ICP audience report')
    expect(get.mock.calls.some(([u]) => String(u).startsWith('/user/icp-audience-report'))).toBe(false)
    fireEvent.click(run)
    expect(await screen.findByText(/Insufficient data: n=3, at least 5 needed/)).toBeTruthy()
    expect(screen.getByText(/CTOs: n=2 \(insufficient data\)/)).toBeTruthy()
    expect(screen.getByText(/2026-11: n=3 \(insufficient data\)/)).toBeTruthy()
    expect(screen.getByText(/Not measured: engager job titles/)).toBeTruthy()
  })

  it('shows shares with n when the floor is met', async () => {
    answer(EMPTY_REPORT, {
      readable: true, min_n: 5,
      overall: { engagers: 6, n: 5, inside: 3, share: 0.6, insufficient: false },
      segments: [], gap_by_month: [{ month: '2026-11', n: 5, share: 0.6, gap: 0.4, insufficient: false }],
      not_measured: [],
    })
    mount()
    fireEvent.click(await screen.findByText('Run the ICP audience report'))
    await waitFor(() => expect(screen.getByText(/60% of engagers with a fit reading fit your ICP \(n=5 of 6\)/)).toBeTruthy())
    expect(screen.getByText(/2026-11: n=5, 60% fit your ICP, 40% do not/)).toBeTruthy()
  })

  it('an unreadable ICP report is an error, not an empty audience', async () => {
    answer(EMPTY_REPORT, { readable: false, min_n: 5, overall: null, segments: [], gap_by_month: [], not_measured: [] })
    mount()
    fireEvent.click(await screen.findByText('Run the ICP audience report'))
    expect(await screen.findByText(/could not be read/)).toBeTruthy()
  })
})
