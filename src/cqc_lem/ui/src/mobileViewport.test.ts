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

// Issue #1556 added the third shape, and it is the one that cost the whole front page: an element
// that asks for more inline room than the screen has does not just overflow, it re-sizes the PAGE.
// A phone browser lays the document out at its min-content width when that is wider than the
// viewport and zooms the result down to fit, so one 640px table floor rendered every band on the
// front page at ~610px on a 375px screen. The invariants in the second block below are all about
// what a component CONTRIBUTES to that min-content width, which is why none of them is visible in
// a render test.

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

/** Every quoted `className` in a file, paired with the tag it was written on. */
function classNamesByTag(source: string): { tag: string; value: string }[] {
  const pairs: { tag: string; value: string }[] = []
  const attribute = /className=(["'])([^"']*)\1/g
  let match: RegExpExecArray | null
  while ((match = attribute.exec(source)) !== null) {
    const opening = source.lastIndexOf('<', match.index)
    const tag = /^<([A-Za-z][\w.]*)/.exec(source.slice(opening, match.index))?.[1] ?? ''
    pairs.push({ tag, value: match[2] })
  }
  return pairs
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

describe('page-width invariants (issue #1556)', () => {
  // Clipping the overflow is not the same as not asking for the room: a scroll container still
  // reports its contents' min-content width to its ancestors, and that is what a phone browser
  // sizes the page from. `contain: inline-size` is the one declaration that stops it.
  it('keeps the table floor from widening the page it sits on', () => {
    const source = files.find(({ rel }) => rel === 'components/TableScroll.tsx')?.source ?? ''
    expect(source, 'TableScroll.tsx is where the scroll region is defined').not.toBe('')
    expect(source).toContain("contain: 'inline-size'")
  })

  // `hidden` and `inline-flex` are the same property at the same specificity, so which one wins is
  // decided by the order Tailwind emits them — and it emits `inline-flex` last. A component whose
  // own base classes set a display therefore cannot be hidden by a caller's class; the nav's trial
  // CTA read as hidden below `sm` from the day the page shipped and never was.
  it('never asks a CtaButton to hide itself through its className', () => {
    const offenders = files
      .filter(({ source }) =>
        classNamesByTag(source).some(
          ({ tag, value }) => tag === 'CtaButton' && /\bhidden\b/.test(value),
        ),
      )
      .map(({ rel }) => rel)
    expect(offenders, 'hide the wrapper around a CtaButton, not the button').toEqual([])
  })

  // A grid item's automatic minimum size is its min-content width, so a column holding a panel of
  // non-wrapping rows cannot shrink to the phone unless it is told it may.
  it('lets every grid column holding a product panel shrink below its content', () => {
    const offenders = files
      .filter(({ source }) => /<ProductPanel/.test(source))
      .filter(({ source }) => !/min-w-0/.test(source))
      .map(({ rel }) => rel)
    expect(offenders, 'a grid column around <ProductPanel> needs `min-w-0`').toEqual([])
  })
})
