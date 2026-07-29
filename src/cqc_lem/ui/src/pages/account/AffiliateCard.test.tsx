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
vi.mock('../../contexts/AuthContext', () => ({ useAuth: () => ({ sessionToken: 'tok' }) }))
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
