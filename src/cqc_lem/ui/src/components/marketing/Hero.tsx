import { useAuth } from '../../contexts/useAuth'
import { TRIAL_DAYS } from '../../constants/plans'
import CtaButton from './CtaButton'
import ProductPanel from './ProductPanel'
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
          size is its min-content width, and ProductPanel's rows hold non-wrapping (`truncate`)
          titles, so the panel column asked for 498px on a 375px phone and took the page with it. */}
      <div className="grid items-start gap-12 lg:grid-cols-12 lg:items-stretch">
        <div className="min-w-0 lg:col-span-5">
          <h1 className="text-section-lg font-semibold text-balance text-fg sm:text-hero">
            LinkedIn posts in your voice, published only after your approval
          </h1>
          {/* Held to the 30-word hero budget (design pass r2). Scheduling, the month-long plan and
              follow-ups are stated in full in the feature beats; the three postures below are
              pinned by Landing.test.tsx. 16px on a phone (the pack's phone body size), so the
              paragraph stays at four lines at 390px. */}
          <p className="mt-6 max-w-measure text-base leading-relaxed text-fg-muted sm:text-lg">
            LEM publishes each post only after you approve it. It suggests comments and replies,
            which you review and send yourself. LEM never asks for your LinkedIn password.
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
          {/* Stretched to the copy column so the frame ends on the trial line, not 50px above it. */}
          <ProductPanel variant="hero" />
        </div>
      </div>
    </Section>
  )
}
