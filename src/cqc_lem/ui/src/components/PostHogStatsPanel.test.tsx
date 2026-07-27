import { describe, expect, it, vi, afterEach } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'
import PostHogStatsPanel from './PostHogStatsPanel'
import { usePostHogStats } from '../hooks/usePostHogStats'

vi.mock('../hooks/usePostHogStats', () => ({ usePostHogStats: vi.fn() }))

const mockedUseStats = vi.mocked(usePostHogStats)

const UNAVAILABLE = { available: false, rows: [] }

function statsResult(data: unknown, isLoading = false) {
  // Only the two fields the component reads — cast avoids re-declaring react-query's full
  // UseQueryResult shape for a value this test only feeds back in.
  return { data, isLoading } as ReturnType<typeof usePostHogStats>
}

afterEach(cleanup)

describe('PostHogStatsPanel (issue #654)', () => {
  it('renders nothing while loading', () => {
    mockedUseStats.mockReturnValue(statsResult(undefined, true))
    const { container } = render(<PostHogStatsPanel />)
    expect(container.innerHTML).toBe('')
  })

  it('renders nothing when every panel is unavailable', () => {
    mockedUseStats.mockReturnValue(
      statsResult({
        posts_engagement: UNAVAILABLE,
        comment_activity: UNAVAILABLE,
        llm_cost_by_feature: UNAVAILABLE,
      })
    )
    const { container } = render(<PostHogStatsPanel />)
    expect(container.innerHTML).toBe('')
  })

  it('renders the latest week and top LLM cost features when data is available', () => {
    mockedUseStats.mockReturnValue(
      statsResult({
        posts_engagement: {
          available: true,
          rows: [
            { week: '2026-07-13', posts_measured: 2, median_engagement_rate: 0.03, impressions: 500 },
            { week: '2026-07-20', posts_measured: 3, median_engagement_rate: 0.05, impressions: 900 },
          ],
        },
        comment_activity: {
          available: true,
          rows: [{ week: '2026-07-20', comments_measured: 12, author_replies: 4, author_reply_pct: 33.33 }],
        },
        llm_cost_by_feature: {
          available: true,
          rows: [
            { feature: 'content', spend_usd: 4.5, calls: 40 },
            { feature: 'comment', spend_usd: 1.25, calls: 20 },
          ],
        },
      })
    )
    render(<PostHogStatsPanel />)
    // The LAST row is this week's — 3 posts, not the prior week's 2.
    expect(screen.getByText('3')).toBeTruthy()
    expect(screen.getByText('5.00% median engagement')).toBeTruthy()
    expect(screen.getByText('12')).toBeTruthy()
    expect(screen.getByText('33.33% author replies')).toBeTruthy()
    expect(screen.getByText('content')).toBeTruthy()
    expect(screen.getByText('$4.50')).toBeTruthy()
  })

  it('reports no LLM calls distinctly from the panel being unavailable', () => {
    mockedUseStats.mockReturnValue(
      statsResult({
        posts_engagement: { available: true, rows: [{ week: '2026-07-20', posts_measured: 1 }] },
        comment_activity: UNAVAILABLE,
        llm_cost_by_feature: { available: true, rows: [] },
      })
    )
    render(<PostHogStatsPanel />)
    expect(screen.getByText('No LLM calls in the last 30 days')).toBeTruthy()
    expect(screen.getByText('Not available yet')).toBeTruthy()
  })

  it('shows a per-panel unavailable message rather than hiding the whole card', () => {
    mockedUseStats.mockReturnValue(
      statsResult({
        posts_engagement: { available: true, rows: [{ week: '2026-07-20', posts_measured: 1 }] },
        comment_activity: UNAVAILABLE,
        llm_cost_by_feature: UNAVAILABLE,
      })
    )
    render(<PostHogStatsPanel />)
    expect(screen.getAllByText('Not available yet').length).toBe(2)
  })
})
