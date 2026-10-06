import { describe, expect, it } from 'vitest'
import { isValidHex, mergeKit } from './brandKit'

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
})
