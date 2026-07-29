import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest'
import { zonedDateToUtcStart, zonedDateToUtcEnd, toZonedInputValue, zonedInputToUtcIso } from './datetime'

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
