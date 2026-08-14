import { describe, expect, it } from 'vitest'
import { readdirSync, readFileSync } from 'node:fs'
import { join, sep } from 'node:path'

// The marketing surface may only draw with brand tokens (issue #1300).
//
// A hex-only test would be tautological — the page was never written with raw hexes. The real
// regression is a CLASS NAME: Tailwind v4's `@theme` ADDS to the default palette rather than
// replacing it, so `text-purple-600`, `text-gray-400` and `text-green-500` all still compile after
// the token block lands. Every one of those was a measured contrast failure on the old page
// (2.94:1, 2.54:1, 2.28:1), so nothing stops them coming back except this.

const SRC = join(process.cwd(), 'src') + sep

// The files that make up the logged-out surface. Kept explicit rather than a glob over `src/`:
// the authenticated app is deliberately NOT in scope (issue #1300 must not restyle Dashboard,
// ContentStudio or Account), and a wildcard would quietly pull them in.
const MARKETING_DIRS = ['components/marketing']
const MARKETING_FILES = [
  'pages/Landing.tsx',
  'pages/FAQ.tsx',
  'components/TutorialVideos.tsx',
  'components/BrandShowcase.tsx',
]

const DEFAULT_PALETTE =
  '(?:slate|gray|zinc|neutral|stone|red|orange|amber|yellow|lime|green|emerald|teal|cyan|sky|blue|' +
  'indigo|violet|purple|fuchsia|pink|rose)'
const UTILITY_PREFIX = '(?:text|bg|border|ring|from|via|to|decoration|outline)'
const BANNED_CLASS = new RegExp(`\\b${UTILITY_PREFIX}-${DEFAULT_PALETTE}-\\d{2,3}\\b`, 'g')
// `#054DB1` belongs in index.css and brandTokens.ts, never inline in a component. Six or eight
// digits only: a three-digit shorthand is indistinguishable from the issue numbers these files are
// full of ("#1300", "#506"), and every colour in this palette is written long-form anyway.
const RAW_HEX = /#(?:[0-9a-fA-F]{8}|[0-9a-fA-F]{6})\b/g

function filesUnder(dir: string): string[] {
  return readdirSync(dir, { withFileTypes: true }).flatMap((entry) => {
    const full = join(dir, entry.name)
    if (entry.isDirectory()) return filesUnder(full)
    if (!/\.tsx?$/.test(entry.name) || entry.name.includes('.test.')) return []
    return [full]
  })
}

const marketingSources = [
  ...MARKETING_DIRS.flatMap((dir) => filesUnder(join(SRC, dir))),
  ...MARKETING_FILES.map((rel) => join(SRC, rel)),
].map((path) => ({
  rel: path.slice(SRC.length).split(sep).join('/'),
  source: readFileSync(path, 'utf8'),
}))

describe('marketing surface colour allowlist (issue #1300)', () => {
  it('finds the marketing sources', () => {
    expect(marketingSources.length).toBeGreaterThan(10)
    expect(marketingSources.map((f) => f.rel)).toContain('pages/Landing.tsx')
    expect(marketingSources.map((f) => f.rel)).toContain('components/marketing/SafetySection.tsx')
  })

  // Positive control: a negative grep is worth nothing until it has been shown to still match what
  // it forbids. Same reasoning as the bundle canary in .github/workflows/ui-build.yml.
  it('still recognises the classes it bans', () => {
    const known = ['text-purple-600', 'bg-gray-50', 'text-green-500', 'from-blue-600', 'border-yellow-400']
    for (const cls of known) {
      expect(new RegExp(BANNED_CLASS.source).test(cls), `${cls} no longer matches`).toBe(true)
    }
    for (const allowed of ['text-brand-600', 'bg-surface-50', 'text-ink-900', 'bg-white']) {
      expect(new RegExp(BANNED_CLASS.source).test(allowed), `${allowed} wrongly banned`).toBe(false)
    }
  })

  it('uses no default-palette colour utility', () => {
    const offenders = marketingSources.flatMap(({ rel, source }) =>
      (source.match(BANNED_CLASS) ?? []).map((match) => `${rel}: ${match}`),
    )
    expect(offenders, 'marketing components must draw with brand tokens only').toEqual([])
  })

  it('recognises a hardcoded colour when it sees one', () => {
    expect(new RegExp(RAW_HEX.source).test('color: #054DB1')).toBe(true)
    expect(new RegExp(RAW_HEX.source).test('issue #1300')).toBe(false)
  })

  it('hardcodes no colour value', () => {
    const offenders = marketingSources.flatMap(({ rel, source }) =>
      (source.match(RAW_HEX) ?? []).map((match) => `${rel}: ${match}`),
    )
    expect(offenders, 'colour belongs in index.css @theme, not in a component').toEqual([])
  })
})
