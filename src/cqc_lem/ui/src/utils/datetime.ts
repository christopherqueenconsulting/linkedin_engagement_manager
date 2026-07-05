// Backend datetimes are UTC. They were historically serialized as naive ISO (no 'Z'/offset); the
// API now emits an explicit 'Z', but we still defensively append one when it's missing so
// `new Date()` parses them as UTC rather than the viewer's local time — otherwise every rendered
// time is off by the browser's UTC offset. Times are always shown 12-hour (never 24h).
function toUtcDate(isoString: string): Date {
  const hasOffset = /[zZ]$|[+-]\d\d:?\d\d$/.test(isoString)
  return new Date(hasOffset ? isoString : isoString + 'Z')
}

export function formatInTimezone(
  isoString: string | null | undefined,
  tz: string,
  options?: Intl.DateTimeFormatOptions,
): string {
  if (!isoString) return '—'
  try {
    return new Intl.DateTimeFormat('en-US', {
      timeZone: tz,
      month: 'short',
      day: 'numeric',
      hour: 'numeric',
      minute: '2-digit',
      hour12: true,
      ...options,
    }).format(toUtcDate(isoString))
  } catch {
    return toUtcDate(isoString).toLocaleString('en-US', { hour12: true })
  }
}
