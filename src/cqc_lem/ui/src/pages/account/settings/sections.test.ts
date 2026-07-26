import { describe, expect, it } from 'vitest'
import { SECTIONS, isSectionKey, resolveSection, sectionLabel } from './sections'
import { SETTINGS } from './registry'

describe('settings IA', () => {
  it('has the eight signed-off sections in order', () => {
    expect(SECTIONS.map((s) => s.key)).toEqual([
      'setup', 'voice', 'content', 'targeting', 'volume', 'outreach', 'newsletter', 'billing',
    ])
  })

  it('routes ?section= deep links', () => {
    expect(resolveSection('volume', null)).toBe('volume')
    expect(resolveSection('nonsense', null)).toBe('setup')
  })

  it('keeps every legacy ?tab= link working', () => {
    expect(resolveSection(null, 'account')).toBe('billing')
    expect(resolveSection(null, 'linkedin')).toBe('setup')
    expect(resolveSection(null, 'content')).toBe('content')
    expect(resolveSection(null, 'automation')).toBe('targeting')
  })

  it('prefers ?section= over a stale ?tab=', () => {
    expect(resolveSection('newsletter', 'account')).toBe('newsletter')
  })

  it('falls back to Setup with no parameters at all', () => {
    expect(resolveSection(null, null)).toBe('setup')
    expect(isSectionKey(null)).toBe(false)
    expect(sectionLabel('voice')).toBe('My Voice')
  })
})

describe('setting registry', () => {
  it('gives every setting the full three-part microcopy contract', () => {
    for (const s of SETTINGS) {
      expect(s.label.length, s.key).toBeGreaterThan(0)
      expect(s.what.length, s.key).toBeGreaterThan(0)
      expect(s.why.length, s.key).toBeGreaterThan(0)
      expect(s.recommended.length, s.key).toBeGreaterThan(0)
    }
  })

  it('has no duplicate keys and only known sections', () => {
    const keys = SETTINGS.map((s) => s.key)
    expect(new Set(keys).size).toBe(keys.length)
    const sections = new Set(SECTIONS.map((s) => s.key))
    for (const s of SETTINGS) expect(sections.has(s.section), s.key).toBe(true)
  })

  // Progressive disclosure: the dense sections (voice, targeting) split into two cards of ≤6, and
  // everything non-essential sits behind the per-section Advanced disclosure.
  it('keeps every section scannable', () => {
    for (const section of SECTIONS) {
      const primary = SETTINGS.filter((s) => s.section === section.key && !s.advanced)
      expect(primary.length, section.key).toBeLessThanOrEqual(9)
    }
  })
})
