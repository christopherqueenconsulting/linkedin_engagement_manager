import { describe, expect, it } from 'vitest'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import {
  APPROVED_PAIRS,
  BRAND_SOURCE,
  THRESHOLDS,
  TOKENS,
  contrastRatio,
  pairRatio,
} from './brandTokens'

// The design contract for issue #1300. Three separate regressions are guarded here, and the reason
// they are source/data tests rather than rendering tests is in brandTokens.ts: jsdom resolves no CSS.

// Derived from the vitest root (src/cqc_lem/ui), same convention as mobileViewport.test.ts —
// `import.meta.url` is an http:// URL under jsdom and fileURLToPath rejects it.
const UI_ROOT = process.cwd()
const REPO_ROOT = join(UI_ROOT, '..', '..', '..')
// The owner-authored package ships one directory per asset family and the names contain spaces.
const PALETTE_FILE = join(REPO_ROOT, 'logo', '06 Color Palette', 'LEM_Brand_Palette.txt')

function readPalette(): string {
  try {
    return readFileSync(PALETTE_FILE, 'utf8')
  } catch (err) {
    // A skipped test here would let the palette file be deleted or renamed and the drift check
    // would silently stop existing, which is the whole failure this test is for.
    throw new Error(`brand palette not readable at ${PALETTE_FILE}`, { cause: err })
  }
}

const indexCss = readFileSync(join(UI_ROOT, 'src', 'index.css'), 'utf8')

function themeBlock(source: string): string {
  const start = source.indexOf('@theme {')
  expect(start, 'index.css must declare an @theme block').toBeGreaterThan(-1)
  const end = source.indexOf('\n}', start)
  return source.slice(start, end)
}

describe('brand tokens are the ones the owner approved (issue #1300)', () => {
  it('reads the palette the brand package ships', () => {
    expect(readPalette()).toContain('LEM Official Palette')
  })

  it('does not drift from logo/06 Color Palette/LEM_Brand_Palette.txt', () => {
    const palette = readPalette()
    for (const [label, hex] of Object.entries(BRAND_SOURCE)) {
      const match = palette.match(new RegExp(`${label}:\\s*(#[0-9A-Fa-f]{6})`))
      expect(match, `"${label}" is missing from ${PALETTE_FILE}`).not.toBeNull()
      expect(match![1].toUpperCase()).toBe(hex.toUpperCase())
    }
  })

  it('carries the four source values into the @theme block', () => {
    const theme = themeBlock(indexCss).toLowerCase()
    expect(theme).toContain('--color-brand-600: #054db1')
    expect(theme).toContain('--color-ink-900: #1f2c37')
    expect(theme).toContain('--color-surface-50: #f9f9f9')
    // White is the page background rather than a declared custom property; Tailwind's own
    // `bg-white` already resolves to it, and re-declaring it would create a second source.
    expect(BRAND_SOURCE.White.toUpperCase()).toBe('#FFFFFF')
  })

  it('keeps every @theme colour identical to the TypeScript mirror', () => {
    const theme = themeBlock(indexCss)
    for (const [name, hex] of Object.entries(TOKENS)) {
      if (name === 'white') continue
      const match = theme.match(new RegExp(`--color-${name}:\\s*(#[0-9A-Fa-f]{6})`))
      expect(match, `--color-${name} is not declared in index.css`).not.toBeNull()
      expect(match![1].toUpperCase(), `--color-${name} drifted from brandTokens.ts`).toBe(
        hex.toUpperCase(),
      )
    }
  })

  it('declares no colour in @theme that the mirror does not know about', () => {
    const declared = [...themeBlock(indexCss).matchAll(/--color-([a-z0-9-]+):/g)].map((m) => m[1])
    expect(declared.length).toBeGreaterThan(10)
    for (const name of declared) {
      expect(Object.keys(TOKENS), `--color-${name} is missing from brandTokens.ts`).toContain(name)
    }
  })
})

describe('contrast contract (WCAG 2.1 AA)', () => {
  it('computes the reference ratios', () => {
    expect(contrastRatio('#FFFFFF', '#000000')).toBeCloseTo(21, 5)
    expect(contrastRatio('#054DB1', '#FFFFFF')).toBeCloseTo(7.77, 1)
  })

  it('clears the threshold on every declared pair', () => {
    const failures = APPROVED_PAIRS.filter((pair) => pairRatio(pair) < THRESHOLDS[pair.role]).map(
      (pair) => `${pair.fg} on ${pair.bg} (${pair.usage}) = ${pairRatio(pair).toFixed(2)}:1`,
    )
    expect(failures, 'declared pairs below their WCAG threshold').toEqual([])
  })

  // Primary blue on charcoal measures 1.83:1. It reads as "the brand colour on the brand colour"
  // and is the single most likely thing for a dark section to reach for.
  it('never declares brand-600 on ink-900', () => {
    expect(APPROVED_PAIRS.find((p) => p.fg === 'brand-600' && p.bg === 'ink-900')).toBeUndefined()
    expect(pairRatio({ fg: 'brand-600', bg: 'ink-900' })).toBeLessThan(3)
  })

  // brand-300 exists FOR charcoal. On white it is 1.93:1, and the fix is to stop using it there —
  // not to darken the token, which would break the surface it was chosen for.
  it('never declares brand-300 on white', () => {
    expect(APPROVED_PAIRS.find((p) => p.fg === 'brand-300' && p.bg === 'white')).toBeUndefined()
    expect(pairRatio({ fg: 'brand-300', bg: 'white' })).toBeLessThan(3)
  })

  it('keeps a focus ring visible on both light surfaces', () => {
    expect(pairRatio({ fg: 'brand-600', bg: 'white' })).toBeGreaterThanOrEqual(3)
    expect(pairRatio({ fg: 'brand-600', bg: 'surface-50' })).toBeGreaterThanOrEqual(3)
  })
})
