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
