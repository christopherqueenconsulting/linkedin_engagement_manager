import { describe, expect, it } from 'vitest'
import { readdirSync, readFileSync } from 'node:fs'
import { join, sep } from 'node:path'

// Issue #894 was reported as "the entire site needs to be mobile friendly", and the two shapes that
// actually broke it are shapes a NEW component reintroduces for free: a full-screen modal panel with
// no height cap, and a data table with no scroll wrapper. Rendering tests only cover the components
// that exist today, so these two invariants are asserted across the whole tree instead.

// Derived from the vitest root (src/cqc_lem/ui), not `import.meta.url` — the jsdom environment
// hands modules an http:// URL, which `fileURLToPath` rejects.
const SRC = join(process.cwd(), 'src') + sep

// LineChart's data table is two columns (date + value) — it fits a 375px phone unwrapped, so
// wrapping it would add a scroll region that never scrolls.
const TABLE_WRAP_EXEMPT = ['components/charts/LineChart.tsx']

function sourceFiles(dir: string): string[] {
  return readdirSync(dir, { withFileTypes: true }).flatMap((entry) => {
    const full = join(dir, entry.name)
    if (entry.isDirectory()) return sourceFiles(full)
    if (!entry.name.endsWith('.tsx') || entry.name.includes('.test.')) return []
    return [full]
  })
}

function count(source: string, needle: RegExp): number {
  return (source.match(needle) ?? []).length
}

const files = sourceFiles(SRC).map((path) => ({
  rel: path.slice(SRC.length).split(sep).join('/'),
  source: readFileSync(path, 'utf8'),
}))

describe('mobile viewport invariants (issue #894)', () => {
  it('finds the SPA sources to check', () => {
    expect(files.length).toBeGreaterThan(20)
  })

  // A landscape phone is ~375px tall. An uncapped panel runs off the bottom of the screen taking its
  // submit button with it, and there is nothing to scroll because the panel itself IS the overflow.
  it('caps and scrolls every full-screen modal panel', () => {
    const offenders = files
      .filter(({ source }) => count(source, /fixed inset-0/g) > 0)
      .filter(({ source }) => {
        const overlays = count(source, /fixed inset-0/g)
        return count(source, /max-h-viewport/g) < overlays
          || count(source, /overflow-y-auto/g) < overlays
      })
      .map(({ rel }) => rel)
    expect(offenders, 'modal panels need `max-h-viewport overflow-y-auto`').toEqual([])
  })

  // A `w-full` table cannot ask for more room than the phone gives it, so every column is squeezed
  // into a sliver — and inside a card that rounds its corners with `overflow-hidden`, the columns
  // that overflow are clipped away entirely. TableScroll is the ONE place that decides this.
  it('wraps every data table in TableScroll', () => {
    const offenders = files
      .filter(({ source }) => /<table/.test(source))
      .filter(({ rel }) => !TABLE_WRAP_EXEMPT.includes(rel))
      .filter(({ source }) => !/from '[^']*TableScroll'/.test(source))
      .map(({ rel }) => rel)
    expect(offenders, 'data tables must be wrapped in <TableScroll>').toEqual([])
  })
})
