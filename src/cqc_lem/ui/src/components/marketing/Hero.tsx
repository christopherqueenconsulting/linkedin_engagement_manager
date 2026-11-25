import { useAuth } from '../../contexts/useAuth'
import { TRIAL_DAYS } from '../../constants/plans'
import CtaButton from './CtaButton'
import Section from './Section'
import { scrollToSection } from './scrollToSection'

// The page's ONE `<h1>` (issue #1300). It names what LEM does — posts it publishes once you approve
// them, engagement it only suggests (#2366) — and the safety posture, because in this category safety is the objection every visitor arrives with and
// the strongest pages answer it in the subhead rather than burying it in an FAQ.
export default function Hero() {
  const { openLoginModal } = useAuth()

  return (
    <Section analyticsName="hero" tone="white" className="pt-12 sm:pt-16">
      {/* `min-w-0` on both columns, not decoration (issue #1556): a grid item's automatic minimum
          size is its min-content width, and a wide child in either column would otherwise take the
          page wider than a 375px phone. */}
      <div className="grid items-start gap-12 lg:grid-cols-12">
        <div className="min-w-0 lg:col-span-5">
          <h1 className="text-section-lg font-semibold text-balance text-fg sm:text-hero">
            Your LinkedIn content, written in your voice&nbsp;— published only when you approve it
          </h1>
          {/* The spec-owned hero copy, kept whole (owner decision 2026-11-24: the density override in
              brand/lem/DESIGN.md). It names content, engagement and the safety posture; the postures
              are pinned by Landing.test.tsx. 16px on a phone (the pack's phone body size). */}
          <p className="mt-6 max-w-measure text-base leading-relaxed text-fg-muted sm:text-lg">
            LEM writes and schedules a month of posts in your voice and publishes each one only after
            you approve it. It also suggests comments, replies and follow-ups, which you review and
            send yourself. LEM never asks for your LinkedIn password.
          </p>
          <div className="mt-8 flex flex-col items-start gap-3 sm:flex-row sm:items-center sm:gap-6">
            <CtaButton
              section="hero"
              cta="hero_trial"
              label="Start free trial from the hero"
              className="w-full sm:w-auto"
              onClick={openLoginModal}
            >
              Start free trial
            </CtaButton>
            <CtaButton
              section="hero"
              cta="hero_how_it_works"
              label="See how it works, from the hero"
              variant="link"
              onClick={() => scrollToSection('how-it-works')}
            >
              See how it works
            </CtaButton>
          </div>
          <p className="mt-4 text-sm text-fg-muted">
            {TRIAL_DAYS}-day free trial. No credit card. Cancel any time.
          </p>
        </div>
        <div className="min-w-0 lg:col-span-7 lg:pt-2">
          {/* The bold element (DESIGN.md Rationale): a real screenshot of the app, not a drawing. It
              is LEM's own Content Studio "Review & Edit" tab, rendered from this repository against
              a local fixture API that serves three sample posts; the crop runs from the tab row to
              the last post (the heading's filled button stays out, so the trial button is the fold's
              one filled action) and nothing is retouched. Provenance: brand/lem/assets.json. The app
              has no dark theme, so both themes show this one light screenshot. Held to 720px (the
              1440px file at 2x) when the hero stacks, so a tablet does not stretch it. */}
          <figure className="max-w-[45rem]">
            {/* Below 600px the same tab is shown as the app lays it out on a phone, so its text stays
                legible instead of shrinking the desktop crop to half size. */}
            <picture>
              <source
                media="(max-width: 599px)"
                srcSet="/product/content-studio-queue-phone-780.webp"
                width={780}
                height={1600}
              />
              <img
                src="/product/content-studio-queue-1440.webp"
                srcSet={[
                  '/product/content-studio-queue-720.webp 720w',
                  '/product/content-studio-queue-1440.webp 1440w',
                ].join(', ')}
                sizes="(min-width: 1024px) 652px, min(720px, calc(100vw - 32px))"
                width={1440}
                height={1312}
                alt={
                  "Screenshot of LEM's Content Studio queue with three sample posts: one pending, " +
                  'one approved and one scheduled.'
                }
                decoding="async"
                fetchPriority="high"
                className="block h-auto w-full rounded-panel border border-hairline shadow-panel"
              />
            </picture>
            <figcaption className="mt-2 text-xs text-fg-muted">Example queue with sample posts</figcaption>
          </figure>
        </div>
      </div>
    </Section>
  )
}
