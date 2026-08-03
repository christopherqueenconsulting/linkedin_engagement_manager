// Data-viz palette tokens (validated reference instance from the `dataviz` skill —
// light-mode values, since the app renders on white cards). The dark steps are kept behind
// the inert `[data-theme="dark"]` hook in each chart's <style>, so charts stay light to match
// the surrounding UI but are correct if a theme toggle is ever added.
export const VIZ = {
  surface: '#fcfcfb',
  gridline: '#e1e0d9',
  baseline: '#c3c2b7',
  textPrimary: '#0b0b0b',
  textSecondary: '#52514e',
  muted: '#898781',
  // Categorical slot 1 (blue) — every chart here is single-series, so one hue is all that's
  // needed and CVD separation is a non-issue.
  series1: '#2a78d6',
} as const

// Compact number formatter for big counts: 1,284 / 12.9K / 4.2M.
export function compactNumber(value: number): string {
  const n = Math.abs(value)
  if (n >= 1_000_000) return `${(value / 1_000_000).toFixed(n >= 10_000_000 ? 0 : 1)}M`
  if (n >= 10_000) return `${(value / 1_000).toFixed(n >= 100_000 ? 0 : 1)}K`
  return value.toLocaleString()
}

// Engagement rate is a per-impression fraction — render as a percentage with 2 decimals.
export function formatRate(value: number): string {
  return `${(value * 100).toFixed(2)}%`
}

// The metric name post_stats stamps on a ranking when it scored impression-normalized rates
// (`METRIC_RATE`); anything else is a weighted per-post count (`METRIC_COUNT`).
export const ENGAGEMENT_RATE_METRIC = 'engagement_rate'

// `avg_engagement` carries BOTH scales — a per-impression fraction in rate mode, a weighted
// per-post count otherwise — so the raw number reads as a bare decimal either way. Only the
// fraction is a percentage: percent-ifying a count would render 40 engagements as "4000%".
export function formatEngagementMetric(value: number, metric?: string | null): string {
  if (!Number.isFinite(value)) return '—'
  if (metric === ENGAGEMENT_RATE_METRIC) return formatRate(value)
  return value.toLocaleString(undefined, { maximumFractionDigits: 1 })
}

// "2026-07-20" → "Jul 20" for compact chart/table axes (dates are tz-agnostic calendar days).
const _MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']
export function shortDate(iso: string): string {
  const [, m, d] = iso.split('-')
  const mi = Number(m) - 1
  return mi >= 0 && mi < 12 ? `${_MONTHS[mi]} ${Number(d)}` : iso
}

export function titleCase(k: string | null): string {
  if (!k) return '—'
  const s = String(k).replace(/[_-]+/g, ' ').trim()
  return s.charAt(0).toUpperCase() + s.slice(1)
}
