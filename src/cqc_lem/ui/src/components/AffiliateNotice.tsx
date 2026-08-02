import { Link } from 'react-router-dom'
import { capture, EVENTS } from '../utils/analytics'
import { useAcknowledgeAffiliateNotice, useAffiliate } from '../hooks/useAffiliate'

/**
 * The enrollment notice (issue #737). Default enrollment is only fair if the user is actually TOLD,
 * so this is the notice — shown once, until acknowledged, and it states the three things the FTC and
 * plain decency require: what they are in, what they get, and how to leave.
 *
 * It offers the opt-out inline as a link to the setting rather than a second "leave" button: the
 * decision belongs on the page that also shows what leaving costs.
 */
export default function AffiliateNotice() {
  const { data } = useAffiliate()
  const acknowledge = useAcknowledgeAffiliateNotice()

  if (!data || !data.program_enabled || !data.enrolled || data.notice_seen_at) return null

  const dismiss = () => {
    capture(EVENTS.affiliateNoticeAcknowledged)
    acknowledge.mutate()
  }

  return (
    <div className="bg-blue-50 border border-blue-200 rounded-lg p-4" data-testid="affiliate-notice">
      <p className="text-sm font-semibold text-blue-900">
        {data.bonus_days > 0
          ? `🎉 You're in the LEM affiliate program — and your trial is ${data.bonus_days} days longer`
          : `🎉 You're in the LEM affiliate program — earn extra trial time for anyone you refer`}
      </p>
      <p className="mt-1 text-sm text-blue-800">
        Every LEM account joins automatically. You get a referral link
        {data.bonus_days > 0 ? `, ${data.bonus_days} bonus trial days for joining,` : ','} and{' '}
        {data.referral_bonus_days} extra trial days each time someone who signs up through your link
        gets set up and posting (up to {data.max_reward_days} days).
      </p>
      <p className="mt-2 text-sm text-blue-800">
        Nothing is posted from your LinkedIn account for this. Promoting LEM from your own account is
        a completely separate setting that stays OFF unless you turn it on yourself.
      </p>
      <p className="mt-2 text-xs text-blue-700">
        Not interested? Leave in one click under{' '}
        <Link to="/account?section=billing" className="underline font-medium">
          Settings → Billing → Affiliate program
        </Link>
        .{' '}
        {data.bonus_days > 0
          ? `Your trial simply returns to the standard ${data.standard_trial_days} days.`
          : 'You keep every trial day you have already earned.'}
      </p>
      <button
        type="button"
        onClick={dismiss}
        disabled={acknowledge.isPending}
        className="mt-3 px-4 py-2 rounded-md bg-blue-600 text-white text-sm font-semibold hover:bg-blue-700 disabled:opacity-50"
      >
        Got it
      </button>
    </div>
  )
}
