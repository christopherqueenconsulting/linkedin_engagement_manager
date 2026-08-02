import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { ReactNode } from 'react'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import AffiliateNotice from './AffiliateNotice'

const get = vi.fn()
const post = vi.fn()

vi.mock('../api/client', () => ({
  default: {
    get: (...args: unknown[]) => get(...args),
    post: (...args: unknown[]) => post(...args),
  },
}))
vi.mock('../contexts/AuthContext', () => ({ useAuth: () => ({ sessionToken: 'tok' }) }))
vi.mock('../utils/analytics', () => ({
  capture: vi.fn(),
  EVENTS: { affiliateNoticeAcknowledged: 'affiliate_notice_acknowledged' },
}))

const BASE = {
  program_enabled: true,
  eligible: true,
  status: 'enrolled',
  enrolled: true,
  referral_code: '42',
  referral_url: 'https://app.lem.test/signup?ref=42',
  notice_seen_at: null,
  referrals: { pending: 0, converted: 0, rejected: 0 },
  days_earned: 0,
  days_from_referrals: 0,
  max_reward_days: 90,
  bonus_days: 0,
  revocable_bonus_days: 0,
  referral_bonus_days: 14,
  standard_trial_days: 14,
  promo_content_opt_in: false,
  promo_consent_at: null,
  promo_consent_version: null,
  promo_content_allowed: false,
  disclosure_text: '#ad — I get free trial time for referrals.',
}

function payload(data: unknown) {
  return { data: { detail: data } }
}

function harness(ui: ReactNode) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>{ui}</MemoryRouter>
    </QueryClientProvider>
  )
}

beforeEach(() => {
  get.mockReset()
  post.mockReset()
})

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('AffiliateNotice (issue #737)', () => {
  it('tells a default-enrolled user what they get and that nothing is posted for them', async () => {
    get.mockResolvedValue(payload(BASE))
    const { container } = harness(<AffiliateNotice />)
    await waitFor(() => expect(screen.getByTestId('affiliate-notice')).toBeTruthy())
    expect(container.textContent).toContain('14 extra trial days')
    expect(container.textContent).toContain('Nothing is posted from your LinkedIn account')
  })

  it('promises no join bonus, and says leaving costs nothing, when the reward is per-referral only', async () => {
    get.mockResolvedValue(payload(BASE))
    const { container } = harness(<AffiliateNotice />)
    await waitFor(() => expect(screen.getByTestId('affiliate-notice')).toBeTruthy())
    expect(container.textContent).not.toContain('0 bonus trial days')
    expect(container.textContent).toContain('You keep every trial day you have already earned')
  })

  it('frames the opt-out as a return to the standard trial when a join bonus IS configured', async () => {
    get.mockResolvedValue(payload({ ...BASE, bonus_days: 7, revocable_bonus_days: 7 }))
    const { container } = harness(<AffiliateNotice />)
    await waitFor(() => expect(screen.getByTestId('affiliate-notice')).toBeTruthy())
    expect(container.textContent).toContain('7 bonus trial days for joining')
    expect(container.textContent).toContain('returns to the standard 14 days')
    expect(container.textContent).not.toContain('lose')
  })

  // The cohort enrolled before the reward policy flipped: joining pays 0 now, but they still HOLD
  // a +7 grant that opting out claws back. Driving this line off `bonus_days` would promise them a
  // free exit and then take a week of trial off them.
  it('warns the grandfathered cohort that leaving still returns them to the standard trial', async () => {
    get.mockResolvedValue(payload({ ...BASE, bonus_days: 0, revocable_bonus_days: 7 }))
    const { container } = harness(<AffiliateNotice />)
    await waitFor(() => expect(screen.getByTestId('affiliate-notice')).toBeTruthy())
    expect(container.textContent).not.toContain('0 bonus trial days')
    expect(container.textContent).not.toContain('You keep every trial day you have already earned')
    expect(container.textContent).toContain('returns to the standard 14 days')
    expect(container.textContent).not.toContain('lose')
  })

  it('stays hidden once acknowledged, and acknowledges on Got it', async () => {
    get.mockResolvedValue(payload({ ...BASE, notice_seen_at: '2026-08-01T00:00:00Z' }))
    harness(<AffiliateNotice />)
    await waitFor(() => expect(get).toHaveBeenCalled())
    expect(screen.queryByTestId('affiliate-notice')).toBeNull()

    cleanup()
    get.mockResolvedValue(payload(BASE))
    post.mockResolvedValue(payload({ ...BASE, notice_seen_at: '2026-08-02T00:00:00Z' }))
    harness(<AffiliateNotice />)
    await waitFor(() => expect(screen.getByText('Got it')).toBeTruthy())
    fireEvent.click(screen.getByText('Got it'))
    await waitFor(() =>
      expect(post).toHaveBeenCalledWith('/user/affiliate/notice', { session_token: 'tok' })
    )
  })
})
