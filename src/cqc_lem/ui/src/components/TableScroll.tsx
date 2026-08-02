import type { ReactNode } from 'react'

// The ONE way a data table survives a narrow screen (issue #894). A `w-full` table has no way to
// say "I need more room than this" — on a phone the browser honours the width and crushes every
// column into unreadable slivers, and a card that rounds its corners with `overflow-hidden` clips
// the right-hand columns away entirely. Wrapping instead keeps a readable floor: the table holds
// `minWidth` and the WRAPPER scrolls sideways.
//
// The wrapper is a labelled, focusable region on purpose — a scroll container that only a mouse
// wheel or a touch drag can reach is unreachable by keyboard (WCAG 2.1.1).
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
      className={`overflow-x-auto ${className}`.trim()}
    >
      <div style={{ minWidth: `${minWidth}px` }}>{children}</div>
    </div>
  )
}
