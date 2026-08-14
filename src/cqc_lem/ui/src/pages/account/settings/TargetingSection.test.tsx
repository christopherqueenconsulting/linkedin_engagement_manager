import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'
import type { EngPrefs, FeedReach } from '../types'
import TargetingSection from './TargetingSection'

const state: { eng: Partial<EngPrefs> | null; setEng: () => void } = { eng: null, setEng: () => {} }

vi.mock('./engagementPrefsCtx', () => ({
  useEngagementPrefs: () => state,
}))

afterEach(cleanup)

function show(feed_sort: FeedReach['feed_sort']) {
  state.eng = {
    include_topics: [],
    exclude_topics: [],
    include_keywords: [],
    exclude_keywords: [],
    include_authors: [],
    exclude_authors: [],
    feed_reach: { examined: 40, passed_filters: 12, matched_topics: 3, commented: 2, fallback_used: false, feed_sort },
  } as unknown as Partial<EngPrefs>
  render(<TargetingSection />)
}

// Issue #817: LEM ranks candidates recency-first, which only holds if LinkedIn's "Sort by → Recent"
// control was there. Without this line the funnel reads the same either way, and a scan of the
// algorithmic feed looks like a scan of the newest posts.
describe('feed reach funnel sort caveat', () => {
  it('says nothing when the scan really was sorted by Recent', () => {
    show('recent')
    expect(screen.queryByTestId('feed-reach-unsorted')).toBeNull()
  })

  it('flags a scan that ran without the sort control', () => {
    show('missing')
    expect(screen.getByTestId('feed-reach-unsorted').textContent).toContain('algorithmic feed')
  })

  it('flags a flip that could not be confirmed', () => {
    show('unknown')
    expect(screen.getByTestId('feed-reach-unsorted')).toBeTruthy()
  })

  it('stays quiet on a surface that never had a sort control', () => {
    show('n/a')
    expect(screen.queryByTestId('feed-reach-unsorted')).toBeNull()
  })

  it('stays quiet for a funnel written before the sort was recorded', () => {
    show(undefined)
    expect(screen.queryByTestId('feed-reach-unsorted')).toBeNull()
  })
})
