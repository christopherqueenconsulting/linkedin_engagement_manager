import type { ReactNode } from 'react'
import { useSectionViewed } from '../../hooks/useSectionViewed'

// One wrapper for every band on the marketing page (issue #1300).
//
// It exists so three things cannot be forgotten section by section: the full-bleed background (the
// old page's "full-bleed" hero was clipped to a 1024px box because it rendered inside the app's
// `<main>`), the inner measure, and the `landing_section_viewed` event.

const TONES = {
  white: 'bg-white text-ink-700',
  tinted: 'bg-surface-50 text-ink-700',
  brand: 'bg-brand-50 text-ink-700',
  dark: 'bg-ink-900 text-white',
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
      data-section={analyticsName ?? id}
      className={`${TONES[tone]} px-4 py-16 sm:py-20 ${className}`.trim()}
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
  onDark = false,
  align = 'center',
}: {
  eyebrow?: string
  title: string
  lede?: ReactNode
  onDark?: boolean
  align?: 'center' | 'left'
}) {
  const alignment = align === 'center' ? 'text-center mx-auto' : 'text-left'
  return (
    <div className={`${alignment} max-w-3xl mb-12`}>
      {eyebrow && (
        <p
          className={`text-sm font-semibold uppercase tracking-wide mb-3 ${
            onDark ? 'text-brand-300' : 'text-brand-600'
          }`}
        >
          {eyebrow}
        </p>
      )}
      <h2
        className={`text-headline sm:text-display font-bold ${onDark ? 'text-white' : 'text-ink-900'}`}
      >
        {title}
      </h2>
      {lede && (
        <p className={`mt-4 text-lg leading-relaxed ${onDark ? 'text-brand-300' : 'text-ink-600'}`}>
          {lede}
        </p>
      )}
    </div>
  )
}
