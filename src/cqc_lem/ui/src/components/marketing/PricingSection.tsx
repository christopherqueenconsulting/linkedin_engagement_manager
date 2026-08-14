import { useAuth } from '../../contexts/AuthContext'
import { INCLUDED_IN_EVERY_PLAN, PLANS, TRIAL_DAYS, rememberPlanIntent } from '../../constants/plans'
import { EVENTS, capture } from '../../utils/analytics'
import CtaButton from './CtaButton'
import { IncludedMark } from './Icon'
import Section, { SectionHeading } from './Section'

// Pricing (issue #1300). Two things changed beyond the styling, and both are honesty fixes:
//
// 1. The prices come from the shared `PLANS` constant, which the account checkout card and the FAQ
//    fallback copy also read. They used to be typed out separately in each of those places.
// 2. There is no per-tier feature table any more. No tier gating exists in the codebase — every
//    account runs the same product — so the old Enterprise column ("Custom AI training",
//    "White-label options", "API access", "Multi-team management") advertised four things that do
//    not exist. The capability list is shared and the tiers differ by price, which is the truth.
export default function PricingSection() {
  const { openLoginModal } = useAuth()

  return (
    <Section id="pricing" analyticsName="pricing" tone="white">
      <SectionHeading
        eyebrow="Pricing"
        title="Every plan is the whole product"
        lede={`Start with a ${TRIAL_DAYS}-day free trial on any plan. No credit card required.`}
      />

      <div className="grid gap-6 sm:grid-cols-2 lg:grid-cols-4">
        {PLANS.map((plan) => (
          <div
            key={plan.tier}
            data-testid={`plan-${plan.tier}`}
            className={`relative flex flex-col rounded-card border bg-white p-6 ${
              // One column on a phone puts the recommended plan first — a visitor who has to scroll
              // past three cards to reach it is being asked to compare from memory.
              plan.recommended ? 'order-first sm:order-none border-brand-600 shadow-card' : 'border-line-200'
            }`}
          >
            {plan.recommended && (
              <span className="absolute -top-3 left-6 rounded-full bg-brand-100 px-3 py-1 text-xs font-bold uppercase tracking-wide text-brand-600">
                Most popular
              </span>
            )}
            <h3 className="text-title font-bold text-ink-900">{plan.label}</h3>
            <p className="mt-3">
              <span className="text-display font-bold text-ink-900">{plan.price}</span>{' '}
              <span className="text-sm text-ink-500">{plan.cadence}</span>
            </p>
            <p className="mt-3 flex-1 text-sm leading-relaxed text-ink-600">{plan.audience}</p>
            <CtaButton
              section="pricing"
              cta={`pricing_${plan.tier}`}
              label={`Start free trial on the ${plan.label} plan`}
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

      <div className="mt-10 rounded-card border border-line-200 bg-surface-50 p-6">
        <h3 className="text-title font-semibold text-ink-900">Included in every plan</h3>
        <ul className="mt-4 grid gap-3 md:grid-cols-2">
          {INCLUDED_IN_EVERY_PLAN.map((item) => (
            <li key={item} className="flex items-start gap-3">
              <IncludedMark included />
              <span className="text-ink-700">{item}</span>
            </li>
          ))}
        </ul>
        <p className="mt-5 text-sm text-ink-500">
          Plans differ by price today, not by feature — there is no capability locked behind a higher
          tier. If that changes, this section changes with it.
        </p>
      </div>
    </Section>
  )
}
