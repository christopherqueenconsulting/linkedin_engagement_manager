import { describe, expect, it } from 'vitest'
import { ENGAGEMENT_RATE_METRIC, formatEngagementMetric, formatRate } from './palette'

// Issue #1004: "avg engagement" rendered the raw number, so an impression-normalized rate read as
// a bare decimal ("avg engagement 0.0612"). The scale is NOT fixed — post_stats emits a fraction
// in rate mode and a weighted count otherwise — so what is asserted here is that the percentage
// is applied to the fraction ONLY.
describe('formatEngagementMetric', () => {
  it('renders a rate as a percentage at fixed precision', () => {
    expect(formatEngagementMetric(0.0612, ENGAGEMENT_RATE_METRIC)).toBe('6.12%')
    expect(formatEngagementMetric(0.06, ENGAGEMENT_RATE_METRIC)).toBe('6.00%')
    expect(formatEngagementMetric(0, ENGAGEMENT_RATE_METRIC)).toBe('0.00%')
    expect(formatEngagementMetric(0.000004, ENGAGEMENT_RATE_METRIC)).toBe('0.00%')
  })

  it('never percent-ifies a per-post count', () => {
    expect(formatEngagementMetric(40, 'engagement')).toBe('40')
    expect(formatEngagementMetric(12.5, 'engagement')).toBe('12.5')
    // A count is rounded to 1 decimal server-side; hold that precision if one ever slips through.
    expect(formatEngagementMetric(12.34567, 'engagement')).toBe('12.3')
    expect(formatEngagementMetric(1234, 'engagement')).toBe('1,234')
  })

  it('treats an absent or unknown metric as a count, not a rate', () => {
    expect(formatEngagementMetric(9)).toBe('9')
    expect(formatEngagementMetric(9, null)).toBe('9')
    expect(formatEngagementMetric(9, 'something_else')).toBe('9')
  })

  it('degrades to a dash rather than printing NaN', () => {
    expect(formatEngagementMetric(Number.NaN, ENGAGEMENT_RATE_METRIC)).toBe('—')
    expect(formatEngagementMetric(Number.POSITIVE_INFINITY)).toBe('—')
    expect(formatEngagementMetric(undefined as unknown as number)).toBe('—')
  })

  it('shares formatRate with the leaderboards so one card cannot drift from another', () => {
    expect(formatEngagementMetric(0.0612, ENGAGEMENT_RATE_METRIC)).toBe(formatRate(0.0612))
  })
})
