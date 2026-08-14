// Date-range presets for the Content Studio post filter. Kept out of the page component so the
// page file exports a component only, and so the pure date maths is unit-testable on its own.

export type DateRangeFilter = 'ALL' | 'today' | 'yesterday' | 'last7days' | 'last30days' | 'thisMonth' | 'next30days' | 'custom'
export const DATE_RANGE_FILTERS: { label: string; value: DateRangeFilter }[] = [
  { label: 'All dates', value: 'ALL' },
  { label: 'Today', value: 'today' },
  { label: 'Yesterday', value: 'yesterday' },
  { label: 'Last 7 days', value: 'last7days' },
  { label: 'Last 30 days', value: 'last30days' },
  { label: 'This month', value: 'thisMonth' },
  { label: 'Next 30 days', value: 'next30days' },
  { label: 'Custom', value: 'custom' },
]

// Return YYYY-MM-DD for *today* in the user's configured timezone so preset ranges
// line up with the wall clock the rest of the UI uses.
export function getTodayInTz(tz: string): string {
  try {
    const parts = new Intl.DateTimeFormat('en-US', {
      timeZone: tz,
      year: 'numeric',
      month: '2-digit',
      day: '2-digit',
    }).formatToParts(new Date())
    const get = (type: string) => parts.find((p) => p.type === type)?.value ?? '0'
    return `${get('year')}-${get('month')}-${get('day')}`
  } catch {
    return new Date().toISOString().slice(0, 10)
  }
}

// Given a preset (or custom) date range, return the user-timezone wall dates that bound it.
export function resolveDateRange(
  preset: DateRangeFilter,
  tz: string,
  customStart?: string,
  customEnd?: string,
): { start: string | null; end: string | null } {
  const today = getTodayInTz(tz)
  const [year, month, day] = today.split('-').map(Number)
  switch (preset) {
    case 'ALL':
      return { start: null, end: null }
    case 'today':
      return { start: today, end: today }
    case 'yesterday': {
      const d = new Date(Date.UTC(year, month - 1, day - 1))
      return { start: d.toISOString().slice(0, 10), end: d.toISOString().slice(0, 10) }
    }
    case 'last7days': {
      const d = new Date(Date.UTC(year, month - 1, day - 6))
      return { start: d.toISOString().slice(0, 10), end: today }
    }
    case 'last30days': {
      const d = new Date(Date.UTC(year, month - 1, day - 29))
      return { start: d.toISOString().slice(0, 10), end: today }
    }
    case 'thisMonth': {
      const start = `${year}-${String(month).padStart(2, '0')}-01`
      const end = new Date(Date.UTC(year, month, 0)).toISOString().slice(0, 10)
      return { start, end }
    }
    case 'next30days': {
      const d = new Date(Date.UTC(year, month - 1, day + 29))
      return { start: today, end: d.toISOString().slice(0, 10) }
    }
    case 'custom':
      return { start: customStart || null, end: customEnd || null }
    default:
      return { start: null, end: null }
  }
}
