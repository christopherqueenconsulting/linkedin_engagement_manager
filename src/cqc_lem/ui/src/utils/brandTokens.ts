// The brand palette as data (issue #1300), so the design contract can be TESTED rather than eyeballed.
//
// `index.css`'s `@theme` block is what the browser reads; this module mirrors it and
// `brandTokens.test.ts` fails the build when the two disagree, when a declared pair drops below its
// WCAG 2.1 threshold, or when one of the two banned pairs is declared at all.
//
// It stays pure TypeScript on purpose: vitest runs jsdom with no CSS pipeline, so Tailwind class
// names are inert strings and `getComputedStyle` never resolves `var(--color-*)`. A test that asked
// the DOM what colour something is would answer "" for every element on the page.

/** The four values sampled from the owner-authored package (PR #1294). */
export const BRAND_SOURCE = {
  'Primary Blue': '#054DB1',
  'Primary Charcoal': '#1F2C37',
  'Light Background': '#F9F9F9',
  White: '#FFFFFF',
} as const

/** Every colour token `@theme` declares, keyed exactly as the CSS custom property is named. */
export const TOKENS = {
  'brand-50': '#EAF2FD',
  'brand-100': '#D6E4FB',
  'brand-300': '#8FBEF7',
  'brand-600': '#054DB1',
  'brand-700': '#043E8E',
  'ink-500': '#5A6B78',
  'ink-600': '#41525F',
  'ink-700': '#33424F',
  'ink-900': '#1F2C37',
  'surface-50': '#F9F9F9',
  'surface-100': '#F1F3F5',
  'line-200': '#DDE3E8',
  'line-300': '#C3CDD6',
  'success-700': '#0B6B3A',
  'success-300': '#7FD3A5',
  'warning-700': '#8A5A00',
  'warning-300': '#F0C46A',
  'danger-700': '#A32014',
  'danger-300': '#F5A79B',
  white: '#FFFFFF',
} as const

export type TokenName = keyof typeof TOKENS

/**
 * WCAG 2.1 threshold a pair has to clear.
 *
 * `large` covers ≥24px text, ≥18.66px bold text and non-text UI (borders, focus rings, icons that
 * carry meaning) — all 3:1 under the same success criteria.
 */
export type ContrastRole = 'body' | 'large'

export const THRESHOLDS: Record<ContrastRole, number> = { body: 4.5, large: 3 }

/** A foreground/background pair the marketing surface is allowed to render, and where. */
export interface ApprovedPair {
  fg: TokenName
  bg: TokenName
  role: ContrastRole
  usage: string
}

/**
 * Every foreground/background combination the marketing surface declares.
 *
 * Adding a colour combination to a component means adding it here first — the test asserts each
 * entry clears its threshold, so a pair that cannot pass never reaches the page.
 */
export const APPROVED_PAIRS: ApprovedPair[] = [
  // Light surfaces
  { fg: 'ink-900', bg: 'white', role: 'body', usage: 'headings on white' },
  { fg: 'ink-900', bg: 'surface-50', role: 'body', usage: 'headings on the tinted band' },
  { fg: 'ink-700', bg: 'white', role: 'body', usage: 'body copy on white' },
  { fg: 'ink-700', bg: 'surface-50', role: 'body', usage: 'body copy on the tinted band' },
  { fg: 'ink-600', bg: 'white', role: 'body', usage: 'secondary copy on white' },
  { fg: 'ink-600', bg: 'surface-50', role: 'body', usage: 'secondary copy on the tinted band' },
  { fg: 'ink-500', bg: 'white', role: 'body', usage: 'microcopy, price cadence, footnotes' },
  { fg: 'ink-500', bg: 'surface-50', role: 'body', usage: 'microcopy on the tinted band' },
  { fg: 'ink-900', bg: 'brand-50', role: 'body', usage: 'copy inside a brand-tinted callout' },
  { fg: 'ink-900', bg: 'surface-100', role: 'body', usage: 'copy inside a neutral callout' },
  { fg: 'brand-600', bg: 'white', role: 'body', usage: 'links, plan names, the focus ring' },
  { fg: 'brand-600', bg: 'surface-50', role: 'body', usage: 'links on the tinted band' },
  { fg: 'brand-600', bg: 'brand-50', role: 'body', usage: 'step numerals in the how-it-works band' },
  { fg: 'brand-700', bg: 'white', role: 'body', usage: 'hovered/pressed link' },
  { fg: 'brand-700', bg: 'brand-50', role: 'body', usage: 'hovered ghost button' },
  { fg: 'success-700', bg: 'white', role: 'body', usage: 'the included ✓ mark and its label' },
  { fg: 'success-700', bg: 'surface-50', role: 'body', usage: 'the included ✓ mark on the tinted band' },
  { fg: 'danger-700', bg: 'white', role: 'body', usage: 'the not-included ✗ mark and its label' },
  { fg: 'danger-700', bg: 'surface-50', role: 'body', usage: 'the not-included ✗ mark on the tinted band' },
  { fg: 'warning-700', bg: 'white', role: 'body', usage: 'the limits-of-automation caveat' },

  // Filled controls
  { fg: 'white', bg: 'brand-600', role: 'body', usage: 'primary button label' },
  { fg: 'white', bg: 'brand-700', role: 'body', usage: 'primary button label, hovered' },
  { fg: 'white', bg: 'ink-900', role: 'body', usage: 'copy on the dark safety/CTA bands' },
  { fg: 'brand-600', bg: 'brand-100', role: 'large', usage: 'MOST POPULAR badge' },

  // Dark surfaces. brand-600 is absent here on purpose — see the test.
  { fg: 'brand-300', bg: 'ink-900', role: 'body', usage: 'accent copy and icons on charcoal' },
  { fg: 'success-300', bg: 'ink-900', role: 'body', usage: 'the ✓ mark on charcoal' },
  { fg: 'danger-300', bg: 'ink-900', role: 'body', usage: 'the ✗ mark on charcoal' },
  { fg: 'warning-300', bg: 'ink-900', role: 'body', usage: 'the caveat on charcoal' },
]

function channelLuminance(channel: number): number {
  const c = channel / 255
  return c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4
}

/** WCAG 2.1 relative luminance of a `#rrggbb` colour. */
export function relativeLuminance(hex: string): number {
  const value = hex.replace('#', '')
  if (!/^[0-9a-fA-F]{6}$/.test(value)) throw new Error(`not a #rrggbb colour: ${hex}`)
  const [r, g, b] = [0, 2, 4].map((i) => channelLuminance(parseInt(value.slice(i, i + 2), 16)))
  return 0.2126 * r + 0.7152 * g + 0.0722 * b
}

/** WCAG 2.1 contrast ratio between two `#rrggbb` colours, 1:1 … 21:1. */
export function contrastRatio(a: string, b: string): number {
  const [la, lb] = [relativeLuminance(a), relativeLuminance(b)]
  const [hi, lo] = la >= lb ? [la, lb] : [lb, la]
  return (hi + 0.05) / (lo + 0.05)
}

/** Contrast of a declared pair, by token name. */
export function pairRatio(pair: Pick<ApprovedPair, 'fg' | 'bg'>): number {
  return contrastRatio(TOKENS[pair.fg], TOKENS[pair.bg])
}
