// Timezone contract (docs/timezone-contract.md): the backend stores and serves every scheduling
// instant in UTC; the SPA renders and edits it in the USER'S configured timezone (Account → Login
// Location, served by /user/timezone), never the browser's. Times are always shown 12-hour.

// Backend datetimes are UTC. They were historically serialized as naive ISO (no 'Z'/offset); the
// API now emits an explicit 'Z', but we still defensively append one when it's missing so
// `new Date()` parses them as UTC rather than the viewer's local time — otherwise every rendered
// time is off by the browser's UTC offset.
function toUtcDate(isoString: string): Date {
  const hasOffset = /[zZ]$|[+-]\d\d:?\d\d$/.test(isoString)
  return new Date(hasOffset ? isoString : isoString + 'Z')
}

// Milliseconds `tz` is ahead of UTC at the given instant (DST-correct, because it asks Intl for the
// wall clock that timezone actually shows at that instant).
function tzOffsetMs(date: Date, tz: string): number {
  const parts = new Intl.DateTimeFormat('en-US', {
    timeZone: tz,
    hour12: false,
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
  }).formatToParts(date)
  const get = (type: string) => Number(parts.find((p) => p.type === type)?.value ?? '0')
  // hour12:false renders midnight as '24' in some engines — normalize it back to 0.
  const asUtc = Date.UTC(get('year'), get('month') - 1, get('day'), get('hour') % 24, get('minute'), get('second'))
  return asUtc - date.getTime()
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

// UTC ISO string -> the `YYYY-MM-DDTHH:mm` value for an <input type="datetime-local">, expressed in
// the user's timezone. A `datetime-local` input has no timezone of its own, so its value MUST be
// the same wall clock formatInTimezone renders next to it — otherwise the editor and the label
// above it show two different times for one post.
export function toZonedInputValue(isoString: string | null | undefined, tz: string): string {
  if (!isoString) return ''
  const d = toUtcDate(isoString)
  if (isNaN(d.getTime())) return ''
  try {
    return new Date(d.getTime() + tzOffsetMs(d, tz)).toISOString().slice(0, 16)
  } catch {
    return ''
  }
}

// The inverse: a `datetime-local` wall clock the user typed, read as being in the user's timezone,
// converted to the explicit-UTC ISO string the API stores. Using `new Date(value).toISOString()`
// instead would interpret it in the BROWSER's timezone, so a user whose Login Location differs from
// their device (travel, VPN, a deliberately different audience timezone) would schedule hours off.
export function zonedInputToUtcIso(value: string | null | undefined, tz: string): string | null {
  if (!value) return null
  // Read the typed wall clock as if it were UTC, then subtract the zone's real offset at that
  // instant. Re-measuring the offset at the candidate instant settles DST boundary cases.
  const wallClockAsUtc = Date.parse(`${value.slice(0, 16)}:00Z`)
  if (isNaN(wallClockAsUtc)) return null
  try {
    let instant = wallClockAsUtc - tzOffsetMs(new Date(wallClockAsUtc), tz)
    instant = wallClockAsUtc - tzOffsetMs(new Date(instant), tz)
    return new Date(instant).toISOString()
  } catch {
    return new Date(wallClockAsUtc).toISOString()
  }
}
