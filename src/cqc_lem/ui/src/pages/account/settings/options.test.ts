import { describe, expect, it } from 'vitest'
import { CADENCE_OPTIONS, DEFAULT_POSTING_DAYS, POST_DAY_PRIORITY, WEEKDAY_OPTIONS, weekdayLabels, weeklyPostSlots } from './options'

// weeklyPostSlots mirrors weekly_post_slots() in utilities/ai/content_framework.py — the SPA has to
// show the same days the planner will actually use, so the two orderings must not drift.
describe('weeklyPostSlots', () => {
  it('fills the day-type calendar in priority order', () => {
    expect(weeklyPostSlots(3, [0, 1, 2, 3, 4, 5, 6])).toEqual([1, 2, 3]) // Tue, Wed, Thu
    expect(weeklyPostSlots(7, [0, 1, 2, 3, 4, 5, 6])).toEqual([0, 1, 2, 3, 4, 5, 6])
  })

  it('narrows to the configured days and caps the cadence by them', () => {
    expect(weeklyPostSlots(3, [0, 2, 4])).toEqual([0, 2, 4])
    expect(weeklyPostSlots(7, [0, 1, 2, 3, 4])).toEqual([0, 1, 2, 3, 4])
    expect(weeklyPostSlots(5, [6])).toEqual([6])
  })

  it('keeps the default cadence off the weekend', () => {
    for (let n = 1; n <= 7; n++)
      expect(weeklyPostSlots(n, DEFAULT_POSTING_DAYS).some((d) => d > 4)).toBe(false)
  })

  it('treats an empty or missing day set as "not configured"', () => {
    expect(weeklyPostSlots(3, [])).toEqual(weeklyPostSlots(3, DEFAULT_POSTING_DAYS))
    expect(weeklyPostSlots(3, null)).toEqual(weeklyPostSlots(3, DEFAULT_POSTING_DAYS))
    expect(weeklyPostSlots(0, DEFAULT_POSTING_DAYS).length).toBe(1)
  })

  it('labels the resolved days for the settings screen', () => {
    expect(weekdayLabels(weeklyPostSlots(3, DEFAULT_POSTING_DAYS))).toBe('Tue, Wed, Thu')
  })
})

describe('cadence + weekday menus', () => {
  it('offers every cadence the API accepts', () => {
    expect(CADENCE_OPTIONS.map((o) => o.value)).toEqual([2, 3, 4, 5, 6, 7])
  })

  it('offers all seven days, Mon=0 first', () => {
    expect(WEEKDAY_OPTIONS.map((o) => o.value)).toEqual([0, 1, 2, 3, 4, 5, 6])
    expect(POST_DAY_PRIORITY.slice().sort((a, b) => a - b)).toEqual([0, 1, 2, 3, 4, 5, 6])
  })
})
