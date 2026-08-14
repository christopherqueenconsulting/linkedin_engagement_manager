import type { ReactNode } from 'react'
import { EVENTS, capture } from '../../utils/analytics'

// Every call to action on the marketing page goes through here (issue #1300), for two reasons that
// both used to be broken:
//
// 1. The old page had five buttons whose entire accessible name was "Get Started". A screen-reader
//    user listing the page's controls heard the same label five times with nothing to tell them
//    apart, so `label` is required and is what is announced.
// 2. A CTA event that does not say WHICH section fired it cannot tell you which section earned the
//    click. `section` is required for the same reason.

export type CtaVariant = 'primary' | 'secondary' | 'ghost' | 'onDark'

// The focus ring colour is per variant, not shared: `outline-offset` puts the ring on the SURROUNDING
// surface, and brand-600 on charcoal measures 1.83:1 — an invisible focus ring on the two dark
// bands. The dark variant rings in white instead.
const VARIANTS: Record<CtaVariant, string> = {
  primary: 'bg-brand-600 text-white hover:bg-brand-700 shadow-card focus-visible:outline-brand-600',
  secondary:
    'border border-line-300 bg-white text-ink-900 hover:border-brand-600 hover:text-brand-700 focus-visible:outline-brand-600',
  ghost:
    'border border-brand-600 text-brand-600 hover:bg-brand-50 hover:text-brand-700 focus-visible:outline-brand-600',
  onDark: 'bg-white text-brand-700 hover:bg-brand-50 focus-visible:outline-white',
}

// 44px is the tap-target floor (WCAG 2.5.5 / the mobile invariant in issue #894). py-3 + text-base
// clears it; a smaller CTA on this page is a bug, so the height is not a prop.
const BASE =
  'inline-flex items-center justify-center gap-2 min-h-11 rounded-lg px-6 py-3 text-base font-semibold ' +
  'transition-colors focus-visible:outline-2 focus-visible:outline-offset-2'

export default function CtaButton({
  label,
  section,
  cta,
  onClick,
  variant = 'primary',
  className = '',
  children,
}: {
  /** Accessible name — must be distinguishable from every other CTA on the page. */
  label: string
  /** Which section this button sits in, as reported to PostHog. */
  section: string
  /** Stable name for the button itself, as reported to PostHog. */
  cta: string
  onClick: () => void
  variant?: CtaVariant
  className?: string
  children?: ReactNode
}) {
  return (
    <button
      type="button"
      aria-label={label}
      data-cta={cta}
      onClick={() => {
        capture(EVENTS.landingCtaClicked, { cta, section })
        onClick()
      }}
      className={`${BASE} ${VARIANTS[variant]} ${className}`.trim()}
    >
      {children ?? label}
    </button>
  )
}
