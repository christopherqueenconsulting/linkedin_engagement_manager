import { describe, expect, it } from 'vitest'
import { isValidHex, mergeKit, shareFromPercent } from './brandKit'

describe('brand kit card helpers', () => {
  it('accepts #rrggbb with or without the hash and nothing else', () => {
    expect(isValidHex('#e9d437')).toBe(true)
    expect(isValidHex('A89816')).toBe(true)
    expect(isValidHex('#fff')).toBe(false)
    expect(isValidHex('#zzzzzz')).toBe(false)
    expect(isValidHex(undefined)).toBe(false)
  })

  it('drops a cleared field instead of sending it blank', () => {
    const kit = { primary_hex: '#e9d437', font_vibe: 'bold', avoid: ['gears'] }
    expect(mergeKit(kit, { font_vibe: '', avoid: [] })).toEqual({ primary_hex: '#e9d437' })
    expect(mergeKit(kit, { accent_hex: '#000000' })).toEqual({ ...kit, accent_hex: '#000000' })
  })

  it('stores the card share as a 0-1 fraction and never an out-of-range one', () => {
    expect(shareFromPercent(40)).toBe(0.4)
    expect(shareFromPercent(0)).toBe(0)
    expect(shareFromPercent(100)).toBe(1)
    expect(shareFromPercent(101)).toBeUndefined()
    expect(shareFromPercent(Number.NaN)).toBeUndefined()
    expect(mergeKit({ card_share: 0.4 }, { card_share: 0 })).toEqual({ card_share: 0 })
  })
})
