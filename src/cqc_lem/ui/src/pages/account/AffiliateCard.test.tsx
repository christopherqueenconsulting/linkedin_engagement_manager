import { describe, expect, it, vi, afterEach, beforeEach } from 'vitest'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import AffiliateCard from './AffiliateCard'
import {
  useAffiliate,
  useSetAffiliatePromoConsent,
  useSetAffiliateStatus,
} from '../../hooks/useAffiliate'

vi.mock('../../hooks/useAffiliate', () => ({
  useAffiliate: vi.fn(),
  useSetAffiliateStatus: vi.fn(),
  useSetAffiliatePromoConsent: vi.fn(),
}))
vi.mock('../../utils/analytics', () => ({
  capture: vi.fn(),
  EVENTS: { referralLinkCopied: 'referral_link_copied' },
}))

const mockedUseAffiliate = vi.mocked(useAffiliate)
const mockedSetStatus = vi.mocked(useSetAffiliateStatus)
const mockedSetPromo = vi.mocked(useSetAffiliatePromoConsent)

const BASE = {
  program_enabled: true,
  eligible: true,
  status: 'enrolled' as const,
  enrolled: true,
  referral_code: '42',
  referral_url: 'https://app.lem.test/signup?ref=42',
  notice_seen_at: null,
  referrals: { pending: 1, converted: 2, rejected: 0 },
  days_earned: 35,
  days_from_referrals: 28,
  max_reward_days: 90,
  bonus_days: 7,
  referral_bonus_days: 14,
  standard_trial_days: 14,
  promo_content_opt_in: false,
  promo_consent_at: null,
  promo_consent_version: null,
  promo_content_allowed: false,
  disclosure_text: '#ad — I get free trial time for referrals.',
}

function affiliateResult(data: unknown) {
  return { data } as ReturnType<typeof useAffiliate>
}

let statusMutate: ReturnType<typeof vi.fn>
let promoMutate: ReturnType<typeof vi.fn>

beforeEach(() => {
  statusMutate = vi.fn()
  promoMutate = vi.fn()
  mockedSetStatus.mockReturnValue({
    mutate: statusMutate, isPending: false, isError: false, isSuccess: false,
  } as unknown as ReturnType<typeof useSetAffiliateStatus>)
  mockedSetPromo.mockReturnValue({
    mutate: promoMutate, isPending: false, isError: false,
  } as unknown as ReturnType<typeof useSetAffiliatePromoConsent>)
})

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('AffiliateCard (issue #737)', () => {
  it('renders nothing when the program is off for this deployment', () => {
    mockedUseAffiliate.mockReturnValue(affiliateResult({ ...BASE, program_enabled: false }))
    const { container } = render(<AffiliateCard />)
    expect(container.innerHTML).toBe('')
  })

  it('shows the referral link and what a referral is worth', () => {
    mockedUseAffiliate.mockReturnValue(affiliateResult(BASE))
    const { container } = render(<AffiliateCard />)
    expect((screen.getByTestId('referral-link') as HTMLInputElement).value).toBe(BASE.referral_url)
    expect(container.textContent).toContain('14 extra trial days')
    expect(container.textContent).toContain('90 days in total')
  })

  it('opts out in ONE click — no confirmation dance', () => {
    mockedUseAffiliate.mockReturnValue(affiliateResult(BASE))
    render(<AffiliateCard />)
    fireEvent.click(screen.getAllByRole('switch')[0])
    expect(statusMutate).toHaveBeenCalledWith(false)
  })

  it('(B) needs an explicit confirmation before it is ever enabled', () => {
    mockedUseAffiliate.mockReturnValue(affiliateResult(BASE))
    render(<AffiliateCard />)

    // Flipping the toggle only reveals the consent step — nothing is saved yet.
    fireEvent.click(screen.getAllByRole('switch')[1])
    expect(promoMutate).not.toHaveBeenCalled()

    fireEvent.click(screen.getByText(/I agree/))
    expect(promoMutate).toHaveBeenCalledWith(true)
  })

  it('shows the exact disclosure that will be attached to promotional posts', () => {
    mockedUseAffiliate.mockReturnValue(affiliateResult(BASE))
    const { container } = render(<AffiliateCard />)
    expect(container.textContent).toContain(BASE.disclosure_text)
  })

  it('turns (B) off immediately, with no confirmation', () => {
    mockedUseAffiliate.mockReturnValue(
      affiliateResult({ ...BASE, promo_content_opt_in: true, promo_consent_at: '2026-07-27T00:00:00Z' })
    )
    render(<AffiliateCard />)
    fireEvent.click(screen.getAllByRole('switch')[1])
    expect(promoMutate).toHaveBeenCalledWith(false)
  })

  it('frames leaving as a return to the standard trial, never as losing days', () => {
    mockedUseAffiliate.mockReturnValue(
      affiliateResult({ ...BASE, enrolled: false, status: 'opted_out', referral_url: '' })
    )
    const { container } = render(<AffiliateCard />)
    expect(container.textContent).toContain('standard 14 days')
    expect(container.textContent).not.toContain('lose')
    // The (B) block is gone with the membership it belonged to.
    expect(screen.getAllByRole('switch')).toHaveLength(1)
  })

  it('explains the company-page boundary rather than showing an empty card', () => {
    mockedUseAffiliate.mockReturnValue(
      affiliateResult({ ...BASE, eligible: false, status: 'ineligible', enrolled: false })
    )
    const { container } = render(<AffiliateCard />)
    expect(container.textContent).toContain('company page')
    expect(screen.queryAllByRole('switch')).toHaveLength(0)
  })
})
