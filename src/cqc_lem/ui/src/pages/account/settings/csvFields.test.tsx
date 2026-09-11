import { StrictMode, useEffect, useState, type ComponentType } from 'react'
import { describe, expect, it, afterEach, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import type { EngPrefs } from '../types'
import OutreachSection from './OutreachSection'
import TargetingSection from './TargetingSection'
import VoiceSection from './VoiceSection'

// CsvInput.test.tsx proves the component; this proves the WIRING. Issue #638 was reported against
// the real settings boxes, and a harness of our own making can't catch a section that goes back to
// binding an <input> straight to csv()/parseCsv().
let eng: Partial<EngPrefs> = {}
const subscribers = new Set<() => void>()

vi.mock('./engagementPrefsCtx', () => ({
  useEngagementPrefs: () => {
    const [, rerender] = useState(0)
    useEffect(() => {
      const sub = () => rerender((n) => n + 1)
      subscribers.add(sub)
      return () => { subscribers.delete(sub) }
    }, [])
    return {
      eng,
      setEng: (patch: Partial<EngPrefs>) => {
        eng = { ...eng, ...patch }
        subscribers.forEach((sub) => sub())
      },
      catchupAllowed: 5,
    }
  },
}))

vi.mock('./conflictsCtx', () => ({
  useConflicts: () => ({ findings: [], alertCounts: {}, applyFix: () => {} }),
}))

vi.mock('./userPrefsCtx', () => ({
  useUserPrefs: () => ({ prefs: null, setPrefs: () => {}, effectiveLanguage: null }),
}))

afterEach(() => { cleanup(); eng = {} })

const box = (settingKey: string) =>
  within(screen.getByTestId(`field-${settingKey}`)).getByRole('textbox') as HTMLInputElement

// Every list setting the report covered, and the section that renders it.
const CSV_FIELDS: [string, ComponentType][] = [
  ['include_topics', TargetingSection],
  ['exclude_topics', TargetingSection],
  ['include_keywords', TargetingSection],
  ['exclude_keywords', TargetingSection],
  ['include_authors', TargetingSection],
  ['exclude_authors', TargetingSection],
  ['focus_topics', VoiceSection],
  ['connection_target_authors', OutreachSection],
]

describe('comma-separated settings fields', () => {
  it.each(CSV_FIELDS)('lets a comma and the space after it be typed into %s', (key, Section) => {
    render(<Section />)
    for (const keystroke of ['a', 'ai', 'ai,', 'ai, ', 'ai, s', 'ai, sales']) {
      fireEvent.change(box(key), { target: { value: keystroke } })
      expect(box(key).value).toBe(keystroke)
    }
    expect(eng[key as keyof EngPrefs]).toEqual(['ai', 'sales'])
  })

  it('renders a saved list and leaves the other filters alone while one is edited', () => {
    eng = { include_topics: ['ai', 'sales'] }
    render(<TargetingSection />)
    expect(box('include_topics').value).toBe('ai, sales')
    fireEvent.change(box('exclude_topics'), { target: { value: 'crypto,' } })
    expect(box('exclude_topics').value).toBe('crypto,')
    expect(box('include_topics').value).toBe('ai, sales')
    expect(eng.exclude_topics).toEqual(['crypto'])
  })

  it('tidies on blur and still accepts a fresh separator afterwards', () => {
    render(<TargetingSection />)
    fireEvent.change(box('include_topics'), { target: { value: 'ai,, sales ,' } })
    fireEvent.blur(box('include_topics'))
    expect(box('include_topics').value).toBe('ai, sales')
    fireEvent.change(box('include_topics'), { target: { value: 'ai, sales,' } })
    expect(box('include_topics').value).toBe('ai, sales,')
    expect(eng.include_topics).toEqual(['ai', 'sales'])
  })

  it('survives StrictMode double-rendering', () => {
    eng = { include_topics: ['ai'] }
    render(<StrictMode><TargetingSection /></StrictMode>)
    fireEvent.change(box('include_topics'), { target: { value: 'ai, ' } })
    expect(box('include_topics').value).toBe('ai, ')
  })
})

// Issue #2047: the per-user forbidden-claim list is the same comma-separated control, parked
// behind Advanced, and masked from session replay like every other free-text content editor.
describe('forbidden_claim_terms editor', () => {
  const openAdvanced = () => fireEvent.click(screen.getByRole('button', { name: /Advanced/ }))

  it('is a CSV box behind the Advanced disclosure that publishes the parsed list', () => {
    eng = { forbidden_claim_terms: ['complexity router'] }
    render(<TargetingSection />)
    expect(screen.queryByTestId('field-forbidden_claim_terms')).toBeNull()
    openAdvanced()
    expect(box('forbidden_claim_terms').value).toBe('complexity router')
    for (const keystroke of ['complexity router,', 'complexity router, ', 'complexity router, lem router']) {
      fireEvent.change(box('forbidden_claim_terms'), { target: { value: keystroke } })
      expect(box('forbidden_claim_terms').value).toBe(keystroke)
    }
    expect(eng.forbidden_claim_terms).toEqual(['complexity router', 'lem router'])
    expect(eng.exclude_keywords).toBeUndefined()
  })

  it('is masked from session replay', () => {
    render(<TargetingSection />)
    openAdvanced()
    const input = box('forbidden_claim_terms')
    expect(input.getAttribute('data-ph-mask')).toBe('true')
    expect(input.className).toContain('ph-no-capture')
    // The other filters describe what LEM READS, not what the user cannot say — unmasked as before.
    expect(box('exclude_keywords').getAttribute('data-ph-mask')).toBeNull()
  })
})
