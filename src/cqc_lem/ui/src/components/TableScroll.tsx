import type { ReactNode } from 'react'

// The ONE way a data table survives a narrow screen (issue #894). A `w-full` table has no way to
// say "I need more room than this" — on a phone the browser honours the width and crushes every
// column into unreadable slivers, and a card that rounds its corners with `overflow-hidden` clips
// the right-hand columns away entirely. Wrapping instead keeps a readable floor: the table holds
// `minWidth` and the WRAPPER scrolls sideways.
//
// The wrapper is a labelled, focusable region on purpose — a scroll container that only a mouse
// wheel or a touch drag can reach is unreachable by keyboard (WCAG 2.1.1).
//
// `contain: inline-size` is what keeps that floor INSIDE the region (issue #1556). Clipping the
// overflow is not enough: a scroll container still reports its contents' min-content width upwards,
// so the 640px floor reached the document's own min-content width, and a phone browser's
// shrink-to-fit then laid the WHOLE page out at ~610px and zoomed it down to fit. Measured on the
// live front page at a 375px viewport: `document.body` min-content 672px, and 530px with this one
// declaration. Inline rather than a utility class because it must not depend on which display
// utility Tailwind emits last.
export default function TableScroll({
  children,
  label,
  minWidth = 640,
  className = '',
  testId,
}: {
  children: ReactNode
  /** Announced name of the scrollable region — say what the table holds. */
  label: string
  /** Narrowest width the table stays readable at; below it the wrapper scrolls. */
  minWidth?: number
  className?: string
  testId?: string
}) {
  return (
    <div
      role="region"
      aria-label={label}
      tabIndex={0}
      data-testid={testId}
      style={{ contain: 'inline-size' }}
      className={`overflow-x-auto ${className}`.trim()}
    >
      <div style={{ minWidth: `${minWidth}px` }}>{children}</div>
    </div>
  )
}
