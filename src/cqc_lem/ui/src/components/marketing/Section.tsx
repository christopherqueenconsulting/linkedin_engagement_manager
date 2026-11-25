import type { ReactNode } from 'react'
import { useSectionViewed } from '../../hooks/useSectionViewed'

// One wrapper for every band on the marketing page (issue #1300).
//
// It exists so three things cannot be forgotten section by section: the full-bleed background (the
// old page's "full-bleed" hero was clipped to a 1024px box because it rendered inside the app's
// `<main>`), the inner measure, and the `landing_section_viewed` event.

const TONES = {
  white: 'bg-raised text-fg',
  tinted: 'bg-page text-fg',
  brand: 'bg-page text-fg',
  dark: 'bg-raised text-fg',
} as const

export type SectionTone = keyof typeof TONES

export default function Section({
  id,
  analyticsName,
  tone = 'white',
  width = 'max-w-6xl',
  className = '',
  children,
}: {
  /** DOM id, when the section is an anchor target. */
  id?: string
  /** Name reported as `section` on the view and CTA events. Defaults to `id`. */
  analyticsName?: string
  tone?: SectionTone
  width?: string
  className?: string
  children: ReactNode
}) {
  const ref = useSectionViewed<HTMLElement>(analyticsName ?? id ?? 'unnamed')

  return (
    <section
      id={id}
      ref={ref}
      {...(tone === 'dark' ? { 'data-theme': 'dark' } : {})}
      data-section={analyticsName ?? id}
      className={`${tone === 'dark' ? 'lem-marketing ' : ''}${TONES[tone]} px-4 py-16 sm:py-20 ${className}`.trim()}
    >
      <div className={`${width} mx-auto`}>{children}</div>
    </section>
  )
}

/** The heading pair every section below the hero opens with. */
export function SectionHeading({
  eyebrow,
  title,
  lede,
  align = 'left',
}: {
  eyebrow?: string
  title: string
  lede?: ReactNode
  align?: 'center' | 'left'
}) {
  const alignment = align === 'center' ? 'text-center mx-auto' : 'text-left'
  return (
    <div className={`${alignment} max-w-3xl mb-12`}>
      {eyebrow && (
        <p
          className="text-sm font-semibold mb-3 text-fg-muted"
        >
          {eyebrow}
        </p>
      )}
      <h2
        className="text-section sm:text-section-lg font-semibold text-balance text-fg"
      >
        {title}
      </h2>
      {lede && (
        <p className="mt-4 max-w-measure text-lg leading-relaxed text-fg-muted">
          {lede}
        </p>
      )}
    </div>
  )
}
