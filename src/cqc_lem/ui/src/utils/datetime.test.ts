import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest'
import {
  formatHour12,
  timezoneAbbreviation,
  zonedDateToUtcStart,
  zonedDateToUtcEnd,
  toZonedInputValue,
  zonedInputToUtcIso,
} from './datetime'

describe('zonedDateToUtcStart', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    // DST is in effect in July for America/New_York (EDT, UTC-4)
    vi.setSystemTime(new Date('2026-07-29T12:00:00Z'))
  })

  afterEach(() => {
    vi.useRealTimers()
  })

  it('converts user-timezone wall midnight to UTC', () => {
    expect(zonedDateToUtcStart('2026-07-29', 'America/New_York')).toBe('2026-07-29T04:00:00.000Z')
    expect(zonedDateToUtcStart('2026-07-29', 'UTC')).toBe('2026-07-29T00:00:00.000Z')
  })

  it('returns null for empty input', () => {
    expect(zonedDateToUtcStart('', 'America/New_York')).toBeNull()
    expect(zonedDateToUtcStart(null, 'America/New_York')).toBeNull()
  })
})

describe('zonedDateToUtcEnd', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date('2026-07-29T12:00:00Z'))
  })

  afterEach(() => {
    vi.useRealTimers()
  })

  it('converts user-timezone wall end-of-day to UTC', () => {
    // 23:59:59.999 EDT -> 03:59:59.999 UTC the next calendar day
    expect(zonedDateToUtcEnd('2026-07-29', 'America/New_York')).toBe('2026-07-30T03:59:59.999Z')
    expect(zonedDateToUtcEnd('2026-07-29', 'UTC')).toBe('2026-07-29T23:59:59.999Z')
  })

  it('returns null for empty input', () => {
    expect(zonedDateToUtcEnd('', 'America/New_York')).toBeNull()
  })
})

describe('round-trip datetime-local conversion', () => {
  it('zonedInputToUtcIso and toZonedInputValue are inverses for a known instant', () => {
    const tz = 'America/New_York'
    const utcIso = '2026-07-29T14:00:00.000Z'
    const inputValue = toZonedInputValue(utcIso, tz)
    expect(inputValue).not.toBe('')
    expect(zonedInputToUtcIso(inputValue, tz)).toBe(utcIso)
  })
})

describe('formatHour12', () => {
  it('renders every hour-of-day 12-hour, never 24-hour', () => {
    expect(formatHour12(0)).toBe('12:00 AM')
    expect(formatHour12(9)).toBe('9:00 AM')
    expect(formatHour12(11)).toBe('11:00 AM')
    expect(formatHour12(12)).toBe('12:00 PM')
    expect(formatHour12(13)).toBe('1:00 PM')
    expect(formatHour12(16)).toBe('4:00 PM')
    expect(formatHour12(23)).toBe('11:00 PM')
  })

  it('never emits a 24-hour clock face for any hour in range', () => {
    for (let h = 0; h < 24; h++) {
      expect(formatHour12(h)).toMatch(/^(1[0-2]|[1-9]):00 (AM|PM)$/)
    }
  })

  it('normalizes out-of-range hours instead of rendering nonsense', () => {
    expect(formatHour12(24)).toBe('12:00 AM')
    expect(formatHour12(25)).toBe('1:00 AM')
    expect(formatHour12(-1)).toBe('11:00 PM')
    expect(formatHour12(13.7)).toBe('1:00 PM')
  })
})

describe('timezoneAbbreviation', () => {
  it('labels a zone with its short name at the given instant', () => {
    // Same zone, opposite sides of a DST boundary — the label has to follow.
    expect(timezoneAbbreviation('America/New_York', new Date('2026-07-29T12:00:00Z'))).toBe('EDT')
    expect(timezoneAbbreviation('America/New_York', new Date('2026-01-15T12:00:00Z'))).toBe('EST')
  })

  it('falls back to the IANA id when the zone is unusable', () => {
    expect(timezoneAbbreviation('Not/AZone')).toBe('Not/AZone')
  })
})
