import { useAuth } from '../../contexts/useAuth'
import { TRIAL_DAYS } from '../../constants/plans'
import CtaButton from './CtaButton'
import ProductPanel from './ProductPanel'
import Section from './Section'
import { scrollToSection } from './scrollToSection'

// The page's ONE `<h1>` (issue #1300). It names both pillars — content and engagement — and the
// safety posture, because in this category safety is the objection every visitor arrives with and
// the strongest pages answer it in the subhead rather than burying it in an FAQ.
export default function Hero() {
  const { openLoginModal } = useAuth()

  return (
    <Section analyticsName="hero" tone="white" className="pt-12 sm:pt-16">
      {/* `min-w-0` on both columns, not decoration (issue #1556): a grid item's automatic minimum
          size is its min-content width, and ProductPanel's rows hold non-wrapping (`truncate`)
          titles, so the panel column asked for 498px on a 375px phone and took the page with it. */}
      <div className="grid items-center gap-12 lg:grid-cols-2">
        <div className="min-w-0">
          <h1 className="text-4xl sm:text-5xl font-bold leading-tight tracking-tight text-ink-900">
            Your LinkedIn content and engagement, running every day — inside the limits you set
          </h1>
          <p className="mt-6 text-lg leading-relaxed text-ink-600">
            LEM writes and schedules a month of posts in your voice, then comments, replies and
            follows up for you. It works from your own session at a human pace, stops at the caps you
            choose, and publishes nothing you have not approved.
          </p>
          <div className="mt-8 flex flex-col gap-3 sm:flex-row">
            <CtaButton
              section="hero"
              cta="hero_trial"
              label="Start free trial from the hero"
              onClick={openLoginModal}
            >
              Start free trial
            </CtaButton>
            <CtaButton
              section="hero"
              cta="hero_how_it_works"
              label="See how it works, from the hero"
              variant="secondary"
              onClick={() => scrollToSection('how-it-works')}
            >
              See how it works
            </CtaButton>
          </div>
          <p className="mt-4 text-sm text-ink-500">
            {TRIAL_DAYS}-day free trial. No credit card. Cancel any time.
          </p>
        </div>
        <div className="min-w-0 lg:pl-4">
          <ProductPanel variant="queue" />
        </div>
      </div>
    </Section>
  )
}
