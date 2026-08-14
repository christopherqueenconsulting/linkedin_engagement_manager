import { describe, expect, it, vi, afterEach, beforeEach } from 'vitest'
import type { ReactNode } from 'react'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import AffiliateCard from './AffiliateCard'

const get = vi.fn()
const post = vi.fn()

vi.mock('../../api/client', () => ({
  default: {
    get: (...args: unknown[]) => get(...args),
    post: (...args: unknown[]) => post(...args),
  },
}))
vi.mock('../../contexts/useAuth', () => ({ useAuth: () => ({ sessionToken: 'tok' }) }))
vi.mock('../../utils/analytics', () => ({
  capture: vi.fn(),
  EVENTS: { referralLinkCopied: 'referral_link_copied' },
}))

const BASE = {
  program_enabled: true,
  eligible: true,
  status: 'enrolled',
  enrolled: true,
  referral_code: '42',
  referral_url: 'https://app.lem.test/signup?ref=42',
  notice_seen_at: null,
  referrals: { pending: 1, converted: 2, rejected: 0 },
  days_earned: 35,
  days_from_referrals: 28,
  max_reward_days: 90,
  bonus_days: 7,
  revocable_bonus_days: 7,
  referral_bonus_days: 14,
  standard_trial_days: 14,
  promo_content_opt_in: false,
  promo_consent_at: null,
  promo_consent_version: null,
  promo_content_allowed: false,
  disclosure_text: '#ad — I get free LinkedIn Engagement Manager trial time when people sign up through my link.',
}

function payload(data: unknown) {
  return { data: { detail: data } }
}

function harness(ui: ReactNode) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(<QueryClientProvider client={client}>{ui}</QueryClientProvider>)
}

beforeEach(() => {
  get.mockReset()
  post.mockReset()
})

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('AffiliateCard (issue #737)', () => {
  it('renders nothing when the program is off for this deployment', async () => {
    get.mockResolvedValue(payload({ ...BASE, program_enabled: false }))
    const { container } = harness(<AffiliateCard />)
    await waitFor(() => expect(container.innerHTML).toBe(''))
  })

  it('shows the referral link and what a referral is worth', async () => {
    get.mockResolvedValue(payload(BASE))
    const { container } = harness(<AffiliateCard />)
    await waitFor(() =>
      expect((screen.getByTestId('referral-link') as HTMLInputElement).value).toBe(BASE.referral_url)
    )
    expect(container.textContent).toContain('14 extra trial days')
    expect(container.textContent).toContain('90 days in total')
  })

  it('opts out in ONE click — no confirmation dance', async () => {
    get.mockResolvedValue(payload(BASE))
    post.mockResolvedValue(payload({ ...BASE, enrolled: false, status: 'opted_out', referral_url: '' }))
    harness(<AffiliateCard />)
    await waitFor(() => expect(screen.getAllByRole('switch')).toHaveLength(2))
    fireEvent.click(screen.getAllByRole('switch')[0])
    await waitFor(() =>
      expect(post).toHaveBeenCalledWith('/user/affiliate/status', { session_token: 'tok', enrolled: false })
    )
  })

  it('(B) needs an explicit confirmation before it is ever enabled', async () => {
    get.mockResolvedValue(payload(BASE))
    post.mockResolvedValue(payload({ ...BASE, promo_content_opt_in: true, promo_consent_at: '2026-07-27T00:00:00Z' }))
    harness(<AffiliateCard />)
    await waitFor(() => expect(screen.getAllByRole('switch')).toHaveLength(2))

    // Flipping the toggle only reveals the consent step — nothing is saved yet.
    fireEvent.click(screen.getAllByRole('switch')[1])
    expect(post).not.toHaveBeenCalled()

    fireEvent.click(screen.getByText(/I agree/))
    await waitFor(() =>
      expect(post).toHaveBeenCalledWith('/user/affiliate/promo-consent', {
        session_token: 'tok',
        enabled: true,
        consent_acknowledged: true,
      })
    )
  })

  it('shows the exact disclosure that will be attached to promotional posts', async () => {
    get.mockResolvedValue(payload(BASE))
    const { container } = harness(<AffiliateCard />)
    await waitFor(() => expect(container.textContent).toContain(BASE.disclosure_text))
  })

  it('turns (B) off immediately, with no confirmation', async () => {
    get.mockResolvedValue(
      payload({ ...BASE, promo_content_opt_in: true, promo_consent_at: '2026-07-27T00:00:00Z' })
    )
    post.mockResolvedValue(payload({ ...BASE, promo_content_opt_in: false, promo_consent_at: null }))
    harness(<AffiliateCard />)
    await waitFor(() => expect(screen.getAllByRole('switch')).toHaveLength(2))
    fireEvent.click(screen.getAllByRole('switch')[1])
    await waitFor(() =>
      expect(post).toHaveBeenCalledWith('/user/affiliate/promo-consent', {
        session_token: 'tok',
        enabled: false,
        consent_acknowledged: false,
      })
    )
  })

  it('frames leaving as a return to the standard trial, never as losing days', async () => {
    get.mockResolvedValue(
      payload({ ...BASE, enrolled: false, status: 'opted_out', referral_url: '' })
    )
    const { container } = harness(<AffiliateCard />)
    await waitFor(() => {
      expect(container.textContent).toContain('standard 14 days')
      expect(container.textContent).not.toContain('lose')
    })
    // The (B) block is gone with the membership it belonged to.
    expect(screen.getAllByRole('switch')).toHaveLength(1)
  })

  // The two opt-out confirmations, driven off the MUTATION result rather than the query cache.
  // Which one is right depends on whether the flip actually clawed days back, and only the flip
  // response knows that — the #750 cohort holds a +7 enrollment grant that IS revoked, while a
  // user under the shipped per-referral policy loses nothing.
  async function optOut(flip: Record<string, unknown>) {
    get.mockResolvedValue(payload(BASE))
    post.mockResolvedValue(
      payload({ ...BASE, enrolled: false, status: 'opted_out', referral_url: '', ...flip })
    )
    const { container } = harness(<AffiliateCard />)
    await waitFor(() => expect(screen.getAllByRole('switch')).toHaveLength(2))
    fireEvent.click(screen.getAllByRole('switch')[0])
    await waitFor(() => expect(screen.getByTestId('affiliate-trial-note')).toBeTruthy())
    return container
  }

  it('tells a user whose join bonus WAS revoked that they return to the standard trial', async () => {
    const container = await optOut({ reward_days: 7, trial_ends_at: '2026-08-16T00:00:00Z' })
    expect(container.textContent).toContain('returns to the standard 14 days')
    expect(container.textContent).not.toContain('you keep the trial days you earned')
  })

  it('tells a user who lost nothing that they keep the days they earned', async () => {
    const container = await optOut({ reward_days: 0, trial_ends_at: '2026-08-30T00:00:00Z' })
    expect(container.textContent).toContain('you keep the trial days you earned')
    expect(container.textContent).not.toContain('returns to the standard')
    expect(container.textContent).not.toContain('lose')
  })

  // A user who is no longer on a trial gets no date back (quoting a stale one would be worse), and
  // a toggle that moves with no acknowledgement at all is not "the user is notified".
  it('still confirms the flip when there is no trial date to report', async () => {
    const container = await optOut({ reward_days: 0, trial_ends_at: null })
    expect(container.textContent).toContain("You've left the program")
    expect(container.textContent).not.toContain('still ends')
    expect(container.textContent).not.toContain('lose')
  })

  it('never advertises a join bonus when the reward is per-referral only', async () => {
    get.mockResolvedValue(
      payload({ ...BASE, bonus_days: 0, enrolled: false, status: 'opted_out', referral_url: '' })
    )
    const { container } = harness(<AffiliateCard />)
    await waitFor(() =>
      expect(container.textContent).toContain('14 extra trial days for each person you refer')
    )
    expect(container.textContent).not.toContain('0 bonus trial days')
    expect(container.textContent).not.toContain('lose')
  })

  it('explains the company-page boundary rather than showing an empty card', async () => {
    get.mockResolvedValue(
      payload({ ...BASE, eligible: false, status: 'ineligible', enrolled: false })
    )
    const { container } = harness(<AffiliateCard />)
    await waitFor(() => {
      expect(container.textContent).toContain('company page')
      expect(screen.queryAllByRole('switch')).toHaveLength(0)
    })
  })
})
