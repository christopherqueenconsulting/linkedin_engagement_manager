import type { SVGProps } from 'react'

// The marketing page's icon system (issue #1300), replacing the emoji it used to draw with.
//
// An emoji is a FONT glyph: it renders differently on every platform, cannot take the brand colour,
// and is announced by a screen reader as whatever the vendor named it ("robot face"). Every icon
// here is drawn on a 24px grid, strokes in `currentColor` so it inherits the token its container
// already declared, and is `aria-hidden` unless the caller gives it a title — an icon that repeats
// the label beside it should not be read out twice.

const PATHS = {
  // Product / capability
  calendar: 'M8 2v4M16 2v4M3 10h18M5 4h14a2 2 0 0 1 2 2v13a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2Z',
  pen: 'M12 20h9M16.5 3.5a2.12 2.12 0 0 1 3 3L7 19l-4 1 1-4Z',
  chat: 'M21 11.5a8.38 8.38 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.38 8.38 0 0 1-3.8-.9L3 21l1.9-5.7a8.38 8.38 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.38 8.38 0 0 1 3.8-.9h.5a8.48 8.48 0 0 1 8 8v.5Z',
  gauge: 'M12 21a9 9 0 1 1 9-9M12 12l5-3',
  shield: 'M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10Z',
  lock: 'M5 11h14v10H5zM8 11V7a4 4 0 0 1 8 0v4',
  // Controls and marks
  check: 'M20 6 9 17l-5-5',
  cross: 'M18 6 6 18M6 6l12 12',
  arrowRight: 'M5 12h14M12 5l7 7-7 7',
  menu: 'M4 7h16M4 12h16M4 17h16',
  close: 'M18 6 6 18M6 6l12 12',
  // Narrative
  slash: 'M12 2v20M4.9 4.9l14.2 14.2',
  clock: 'M12 22a10 10 0 1 0 0-20 10 10 0 0 0 0 20ZM12 6v6l4 2',
  users: 'M17 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2M9.5 11a4 4 0 1 0 0-8 4 4 0 0 0 0 8ZM22 21v-2a4 4 0 0 0-3-3.87M16.5 3.13a4 4 0 0 1 0 7.75',
} as const

export type IconName = keyof typeof PATHS

export default function Icon({
  name,
  title,
  className = 'h-5 w-5',
  ...rest
}: {
  name: IconName
  /** Accessible name. Omit it whenever the icon only repeats adjacent text. */
  title?: string
  className?: string
} & Omit<SVGProps<SVGSVGElement>, 'name' | 'className'>) {
  return (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.75}
      strokeLinecap="round"
      strokeLinejoin="round"
      className={className}
      role={title ? 'img' : undefined}
      aria-hidden={title ? undefined : true}
      focusable="false"
      {...rest}
    >
      {title && <title>{title}</title>}
      <path d={PATHS[name]} />
    </svg>
  )
}

/**
 * The included/not-included mark used by every feature and pricing list.
 *
 * The glyph alone carried the meaning on the old page — a screen reader announced "✓" or nothing at
 * all, and a red/green pair is invisible to the most common colour deficiency. The word rides along
 * in `sr-only` text so the meaning survives both.
 */
export function IncludedMark({ included, onDark = false }: { included: boolean; onDark?: boolean }) {
  const tone = included
    ? onDark
      ? 'text-success-300'
      : 'text-success-700'
    : onDark
      ? 'text-danger-300'
      : 'text-danger-700'
  return (
    <span className={`shrink-0 ${tone}`}>
      <Icon name={included ? 'check' : 'cross'} className="h-5 w-5" />
      <span className="sr-only">{included ? 'included' : 'not included'}</span>
    </span>
  )
}
