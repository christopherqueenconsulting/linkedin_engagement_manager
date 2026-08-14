import { describe, expect, it } from 'vitest'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import {
  INCLUDED_IN_EVERY_PLAN,
  PAID_PLANS,
  PLANS,
  TRIAL_DAYS,
  monthlyPriceLabel,
  paidPricingSentence,
  planFor,
  rememberPlanIntent,
  takePlanIntent,
} from './plans'

// Issue #1300: $29/$79/$199 lived in four places and none of them was the source of truth. These
// tests assert the three SPA surfaces now read one, and that the values themselves did not move —
// changing a price is a Stripe decision, not a refactor.

const SRC = join(process.cwd(), 'src')
const read = (rel: string) => readFileSync(join(SRC, rel), 'utf8')

describe('the plan constant is the SPA\'s one price source (issue #1300)', () => {
  it('keeps every displayed price exactly as it was', () => {
    expect(PLANS.map((plan) => [plan.tier, plan.price])).toEqual([
      ['free_trial', '$0'],
      ['starter', '$29'],
      ['professional', '$79'],
      ['enterprise', '$199'],
    ])
    expect(TRIAL_DAYS).toBe(14)
  })

  it('is imported by all three surfaces that used to hardcode a price', () => {
    for (const rel of [
      'components/marketing/PricingSection.tsx',
      'pages/account/SubscriptionCard.tsx',
      'pages/FAQ.tsx',
    ]) {
      expect(read(rel), `${rel} must read the shared PLANS constant`).toMatch(
        /from '\.\.?\/(\.\.\/)?constants\/plans'/,
      )
    }
  })

  it('leaves no literal price behind on those surfaces', () => {
    for (const rel of [
      'components/marketing/PricingSection.tsx',
      'pages/account/SubscriptionCard.tsx',
      'pages/FAQ.tsx',
      'pages/Landing.tsx',
    ]) {
      const source = read(rel)
      for (const price of ['$29', '$79', '$199']) {
        expect(source.includes(price), `${rel} still hardcodes ${price}`).toBe(false)
      }
    }
  })

  it('names one recommended plan', () => {
    expect(PLANS.filter((plan) => plan.recommended).map((p) => p.tier)).toEqual(['professional'])
  })

  it('offers exactly the three paid tiers for upgrade, in order', () => {
    expect(PAID_PLANS.map((plan) => plan.tier)).toEqual(['starter', 'professional', 'enterprise'])
    expect(monthlyPriceLabel(planFor('professional'))).toBe('$79/mo')
  })

  it('writes the prose form from the same values', () => {
    expect(paidPricingSentence()).toBe(
      'After the trial, Starter is $29/month, Professional is $79/month and Enterprise is $199/month.',
    )
  })

  it('claims no capability that is gated behind a tier', () => {
    // No tier gating exists in the codebase, so a per-tier feature list would be advertising
    // something that does not ship. The capability list is deliberately shared.
    expect(INCLUDED_IN_EVERY_PLAN.length).toBeGreaterThan(3)
    for (const plan of PLANS) {
      expect(Object.keys(plan)).not.toContain('features')
    }
  })
})

describe('plan intent survives signup exactly once', () => {
  it('is read and cleared', () => {
    rememberPlanIntent('professional')
    expect(takePlanIntent()).toBe('professional')
    expect(takePlanIntent()).toBeNull()
  })

  it('ignores a value that is not a tier', () => {
    window.sessionStorage.setItem('lem:plan-intent', 'platinum')
    expect(takePlanIntent()).toBeNull()
  })
})
