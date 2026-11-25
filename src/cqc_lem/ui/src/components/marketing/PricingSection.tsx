import { useAuth } from '../../contexts/useAuth'
import {
  SHOWN_PLANS,
  TRIAL_DAYS,
  rememberPlanIntent,
  tierDifferenceSentence,
} from '../../constants/plans'
import { EVENTS, capture } from '../../utils/analytics'
import CtaButton from './CtaButton'
import Section, { SectionHeading } from './Section'

// Pricing (issue #1300). Two things changed beyond the styling, and both are honesty fixes:
//
// 1. The prices come from the shared `PLANS` constant, which the account checkout card and the FAQ
//    fallback copy also read. They used to be typed out separately in each of those places.
// 2. There is no per-tier feature table any more. Every account runs the same product, so the old
//    Enterprise column ("Custom AI training", "White-label options", "API access", "Multi-team
//    management") advertised four things that do not exist. The capability list is shared and the
//    tiers differ by price plus ONE gate, the catch-up cap, which the note names (issue #2447).
export default function PricingSection() {
  const { openLoginModal } = useAuth()

  return (
    <Section id="pricing" analyticsName="pricing" tone="white">
      <SectionHeading
        title="Every plan runs the whole loop"
        lede={`Start with a ${TRIAL_DAYS}-day free trial on any plan. No credit card required.`}
      />

      {/* `SHOWN_PLANS`, not `PLANS`: during the pricing test (issue #2370) this is the trial plus the
          recommended plan, so two cards split the content width 6/6 rather than sit in half of a
          four-column row. Not centred: the heading and the "Included" panel hang on the same edges. */}
      <div className={`grid gap-6 sm:grid-cols-2 ${SHOWN_PLANS.length > 2 ? 'lg:grid-cols-4' : ''}`}>
        {SHOWN_PLANS.map((plan) => (
          <div
            key={plan.tier}
            data-testid={`plan-${plan.tier}`}
            className={`relative flex flex-col rounded-panel border bg-raised p-6 ${
              // One column on a phone puts the recommended plan first — a visitor who has to scroll
              // past three cards to reach it is being asked to compare from memory.
              plan.recommended ? 'order-first sm:order-none border-accent shadow-panel' : 'border-hairline'
            }`}
          >
            {plan.recommended && (
              <span className="absolute -top-3 left-6 rounded-full bg-accent px-3 py-1 text-xs font-semibold text-on-accent">
                Most popular
              </span>
            )}
            <h3 className="text-title font-bold text-fg">{plan.label}</h3>
            <p className="mt-3">
              <span className="text-section-lg font-semibold text-fg">{plan.price}</span>{' '}
              <span className="text-sm text-fg-muted">{plan.cadence}</span>
            </p>
            <p className="mt-3 flex-1 text-sm leading-relaxed text-fg-muted">{plan.audience}</p>
            <CtaButton
              section="pricing"
              cta={`pricing_${plan.tier}`}
              // The accessible name contains the visible text (WCAG 2.5.3, Label in Name), so a
              // voice-control user who says "click Choose Professional" finds the button.
              label={
                plan.tier === 'free_trial'
                  ? 'Start free trial on the Free Trial plan'
                  : `Choose ${plan.label} and start the free trial`
              }
              variant={plan.recommended ? 'primary' : 'ghost'}
              className="mt-6 w-full"
              onClick={() => {
                capture(EVENTS.landingPlanSelected, { tier: plan.tier, price: plan.price })
                rememberPlanIntent(plan.tier)
                openLoginModal()
              }}
            >
              {plan.tier === 'free_trial' ? 'Start free' : `Choose ${plan.label}`}
            </CtaButton>
          </div>
        ))}
      </div>

      {/* Design pass r3: the "Included in every plan" list is gone (density budget). Its six lines
          restated the feature beats, the safety ledger and the comparison above; the heading and
          this sentence carry the one fact it added, that every plan runs the same product. */}
      <p data-testid="tier-difference" className="mt-8 max-w-measure text-sm text-fg-muted">
        Plans differ by price and one cap. {tierDifferenceSentence()} Everything else is the same on
        every plan.
      </p>
    </Section>
  )
}
