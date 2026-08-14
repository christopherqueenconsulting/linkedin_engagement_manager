import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import api from '../../api/client'
import { useAuth } from '../../contexts/useAuth'
import SettingsCard from '../../components/SettingsCard'
import { PAID_PLANS, PLANS, monthlyPriceLabel, takePlanIntent } from '../../constants/plans'

// Labels and prices come from the shared PLANS constant (issue #1300). This card is the LIVE
// checkout surface, so it and the landing page disagreeing about a price is the expensive version
// of that bug.
const TIER_LABELS: Record<string, string> = Object.fromEntries(
  PLANS.map((plan) => [plan.tier, plan.label]),
)

const TIER_COLORS: Record<string, string> = {
  free_trial: 'bg-gray-100 text-gray-700',
  starter: 'bg-blue-100 text-blue-800',
  professional: 'bg-purple-100 text-purple-800',
  enterprise: 'bg-yellow-100 text-yellow-800',
}

function daysUntil(iso: string | null | undefined): number | null {
  if (!iso) return null
  const diff = new Date(iso).getTime() - Date.now()
  return Math.max(0, Math.floor(diff / (1000 * 60 * 60 * 24)))
}

export default function SubscriptionCard() {
  const { sessionToken } = useAuth()
  const queryClient = useQueryClient()
  const [checkoutMsg, setCheckoutMsg] = useState<{ ok: boolean; text: string } | null>(null)
  const [portalMsg, setPortalMsg] = useState<string | null>(null)
  // The plan a visitor pressed on the marketing page before signing up (issue #1300). Read once
  // per mount and cleared as it is read, so it highlights the first view of this card and no more.
  const [intendedTier] = useState(() => takePlanIntent())

  const { data: settingsData } = useQuery({
    queryKey: ['user-settings', sessionToken],
    queryFn: () =>
      api
        .get(`/user/settings?session_token=${encodeURIComponent(sessionToken!)}`)
        .then((r) => r.data.detail as {
          subscription: {
            status: string | null
            tier: string | null
            trial_started_at: string | null
            trial_ends_at: string | null
            stripe_customer_id: string | null
          } | null
          preferences: {
            last_login_inactivate_delay: number | null
            auto_schedule_posts: boolean
          } | null
          blog_url: string | null
          sitemap_url: string | null
          company_linked_in_url: string | null
        }),
    enabled: !!sessionToken,
    staleTime: 60 * 1000,
  })

  const subscription = settingsData?.subscription
  const tier = subscription?.tier ?? 'free_trial'
  const trialDays = daysUntil(subscription?.trial_ends_at)
  const isOnTrial = subscription?.status === 'trial'
  const isPaidPlan = subscription?.status === 'active'
  const hasStripeCustomer = !!subscription?.stripe_customer_id

  // Stripe checkout redirect
  const checkoutMutation = useMutation({
    mutationFn: (tier: string) =>
      api
        .post('/billing/create-checkout-session', {
          session_token: sessionToken,
          tier,
          success_url: `${window.location.origin}/account?upgraded=1`,
          cancel_url: `${window.location.origin}/account`,
        })
        .then((r) => r.data.detail as { checkout_url: string | null; upgraded?: boolean }),
    onSuccess: (detail) => {
      if (detail.upgraded || !detail.checkout_url) {
        // In-place upgrade — no Stripe redirect needed. Refresh subscription data.
        setCheckoutMsg({ ok: true, text: 'Plan updated successfully!' })
        queryClient.invalidateQueries({ queryKey: ['userSettings'] })
        setTimeout(() => setCheckoutMsg(null), 5000)
      } else {
        window.location.href = detail.checkout_url
      }
    },
    onError: () => {
      setCheckoutMsg({ ok: false, text: 'Could not start checkout — please try again.' })
      setTimeout(() => setCheckoutMsg(null), 6000)
    },
  })

  // Stripe portal redirect
  const portalMutation = useMutation({
    mutationFn: () =>
      api
        .post('/billing/create-portal-session', {
          session_token: sessionToken,
          return_url: `${window.location.origin}/account`,
        })
        .then((r) => r.data.detail.portal_url as string),
    onSuccess: (url) => { window.location.href = url },
    onError: () => setPortalMsg('Could not open billing portal — please try again.'),
  })

  return (
    <SettingsCard>
      <div className="flex items-center justify-between">
        <h2 className="text-base font-semibold text-gray-700">
          Subscription <span className="text-red-500">*</span>
        </h2>
        <span className={`text-xs font-semibold px-2.5 py-1 rounded-full ${TIER_COLORS[tier] ?? TIER_COLORS.free_trial}`}>
          {TIER_LABELS[tier] ?? tier}
        </span>
      </div>

      {isOnTrial && trialDays !== null && (
        <div className={`rounded-lg p-3 text-sm ${trialDays <= 3 ? 'bg-red-50 text-red-800 border border-red-200' : 'bg-yellow-50 text-yellow-800 border border-yellow-200'}`}>
          {trialDays === 0
            ? 'Your free trial has expired. Upgrade to keep using LEM.'
            : `Free trial: ${trialDays} day${trialDays === 1 ? '' : 's'} remaining.`}
        </div>
      )}

      {isPaidPlan && (
        <p className="text-sm text-green-700">Your subscription is active.</p>
      )}

      {/* Upgrade options — shown to trial users without a paid plan */}
      {!isPaidPlan && hasStripeCustomer && (
        <div className="space-y-2">
          <p className="text-xs text-gray-500 font-medium uppercase tracking-wide">Upgrade your plan</p>
          <div className="grid grid-cols-3 gap-2">
            {PAID_PLANS.map((plan) => (
              <button
                key={plan.tier}
                onClick={() => checkoutMutation.mutate(plan.tier)}
                disabled={checkoutMutation.isPending}
                className={`flex flex-col items-center rounded-lg p-3 transition-colors disabled:opacity-50 text-center ${
                  plan.tier === intendedTier
                    ? 'border-2 border-blue-500 bg-blue-50'
                    : 'border border-blue-200 hover:bg-blue-50'
                }`}
              >
                <span className="text-sm font-semibold text-blue-800">{plan.label}</span>
                <span className="text-xs text-gray-500">{monthlyPriceLabel(plan)}</span>
                {plan.tier === intendedTier && (
                  <span className="text-[10px] font-medium text-blue-700">the plan you picked</span>
                )}
              </button>
            ))}
          </div>
          {checkoutMutation.isPending && (
            <p className="text-xs text-blue-600">Redirecting to checkout…</p>
          )}
          {checkoutMsg && (
            <p className={`text-sm font-medium ${checkoutMsg.ok ? 'text-green-600' : 'text-red-600'}`}>
              {checkoutMsg.text}
            </p>
          )}
        </div>
      )}

      {/* Billing portal — shown to paid subscribers */}
      {isPaidPlan && hasStripeCustomer && (
        <button
          onClick={() => portalMutation.mutate()}
          disabled={portalMutation.isPending}
          className="text-sm text-blue-600 hover:underline disabled:opacity-50"
        >
          Manage billing →
        </button>
      )}

      {portalMsg && (
        <p className="text-sm font-medium text-red-600">{portalMsg}</p>
      )}

      {!hasStripeCustomer && (
        <p className="text-xs text-gray-400">
          Billing not yet configured for your account. Contact support if you need help.
        </p>
      )}
    </SettingsCard>
  )
}
