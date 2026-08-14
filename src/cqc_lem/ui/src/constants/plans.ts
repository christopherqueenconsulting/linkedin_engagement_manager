// The ONE place a plan's name and price live in the SPA (issue #1300).
//
// Before this, $29/$79/$199 was written out four times — the landing page, the account checkout
// card, the FAQ fallback copy and a doc — with nothing tying them together, so a price change
// silently disagreed with itself depending on where a visitor read it.
//
// A caveat that has to stay attached to these numbers: `utilities/stripe_util.py` maps a tier to a
// `STRIPE_PRICE_ID_*` env var, so what a customer is actually CHARGED lives in the Stripe dashboard
// and cannot be verified from this repo. These strings are display copy that must be kept in step
// with Stripe by hand.
//
// What is deliberately NOT here is a per-tier feature list. No tier gating exists in the codebase —
// every account runs the same product today — so a table claiming Enterprise unlocks capabilities
// Starter does not would be advertising something that does not exist. The shipped capability list
// is shared (`INCLUDED_IN_EVERY_PLAN`) and the tiers differ by price alone until gating ships.

export type PlanTier = 'free_trial' | 'starter' | 'professional' | 'enterprise'

export interface Plan {
  /** Matches the tier string `POST /billing/create-checkout-session` expects. */
  tier: PlanTier
  label: string
  /** Display price, exactly as it is written on both surfaces. */
  price: string
  /** What the price is per — "per month", "14 days free". */
  cadence: string
  /** Who the tier is for. No capability claim: see the note above. */
  audience: string
  /** The plan the pricing section leads with. */
  recommended?: boolean
}

export const TRIAL_DAYS = 14

export const PLANS: readonly Plan[] = [
  {
    tier: 'free_trial',
    label: 'Free Trial',
    price: '$0',
    cadence: `${TRIAL_DAYS} days free`,
    audience: 'Try the whole product before you decide. No credit card.',
  },
  {
    tier: 'starter',
    label: 'Starter',
    price: '$29',
    cadence: 'per month',
    audience: 'One operator building a presence from scratch.',
  },
  {
    tier: 'professional',
    label: 'Professional',
    price: '$79',
    cadence: 'per month',
    audience: 'Founders and consultants posting and engaging every weekday.',
    recommended: true,
  },
  {
    tier: 'enterprise',
    label: 'Enterprise',
    price: '$199',
    cadence: 'per month',
    audience: 'Teams who want the whole loop running with support behind it.',
  },
] as const

/** The paid tiers, in the order the account page offers them as an upgrade. */
export const PAID_PLANS: readonly Plan[] = PLANS.filter((plan) => plan.tier !== 'free_trial')

export function planFor(tier: PlanTier): Plan {
  const plan = PLANS.find((p) => p.tier === tier)
  if (!plan) throw new Error(`unknown plan tier: ${tier}`)
  return plan
}

/** "$29/mo" — the compact form the account upgrade buttons use. */
export function monthlyPriceLabel(plan: Plan): string {
  return `${plan.price}/mo`
}

/**
 * The capability list, shared by every tier.
 *
 * Each line names something that exists in the codebase today; nothing here is aspirational.
 */
export const INCLUDED_IN_EVERY_PLAN: readonly string[] = [
  'A 30-day buyer-journey content plan, scheduled around your golden hours',
  'Text posts, carousels, native video and newsletter editions in your own voice',
  'Feed commenting, replies, seed comments and appreciation DMs',
  'Per-day caps, targeting rules and human-paced timing you control',
  'Preview and approval before anything is published or sent',
  'Comment outcomes, post and audience stats, and a suppression tripwire',
] as const

// Which plan a visitor pressed on the marketing page, carried across signup (issue #1300).
// Previously all seven landing CTAs opened the same login modal with no plan intent at all, so
// "I clicked Professional" was information the product threw away. sessionStorage, not localStorage:
// the intent belongs to this visit, and a stale intent from last month would highlight the wrong
// plan for someone who has since chosen another.
const PLAN_INTENT_KEY = 'lem:plan-intent'

export function rememberPlanIntent(tier: PlanTier): void {
  try {
    window.sessionStorage.setItem(PLAN_INTENT_KEY, tier)
  } catch {
    // Storage denied (private mode, blocked cookies) — the intent is a nicety, never a blocker.
  }
}

/** Reads and CLEARS the intent, so it colours the first view of the account page and no more. */
export function takePlanIntent(): PlanTier | null {
  try {
    const raw = window.sessionStorage.getItem(PLAN_INTENT_KEY)
    if (!raw) return null
    window.sessionStorage.removeItem(PLAN_INTENT_KEY)
    return PLANS.some((plan) => plan.tier === raw) ? (raw as PlanTier) : null
  } catch {
    return null
  }
}

/**
 * The prices as one sentence, for prose that has to name all three.
 *
 * The FAQ fallback answer uses it so the seeded copy can never drift from the pricing section.
 */
export function paidPricingSentence(): string {
  const [starter, professional, enterprise] = PAID_PLANS
  return (
    `After the trial, ${starter.label} is ${starter.price}/month, ` +
    `${professional.label} is ${professional.price}/month and ` +
    `${enterprise.label} is ${enterprise.price}/month.`
  )
}
