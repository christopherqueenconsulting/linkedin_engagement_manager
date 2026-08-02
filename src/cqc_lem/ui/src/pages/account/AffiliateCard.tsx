import { useState } from 'react'
import Toggle from '../../components/Toggle'
import { capture, EVENTS } from '../../utils/analytics'
import {
  useAffiliate,
  useSetAffiliatePromoConsent,
  useSetAffiliateStatus,
} from '../../hooks/useAffiliate'

/**
 * Account > Billing > Affiliate program (issue #737).
 *
 * TWO toggles, rendered as two visually separate blocks, because they are two different asks and
 * conflating them is the failure mode the issue is written against:
 *
 *  - (A) Affiliate status — on by default, one click to leave. Leaving returns the user to the
 *    standard trial; the copy says exactly that, and never frames it as losing days they own.
 *  - (B) Promotional posts from the user's OWN LinkedIn account — off, and turning it on is an
 *    explicit consent action with its own explanation, its own confirmation, and the FTC disclosure
 *    shown verbatim so the user can see what will be attached to their posts.
 */
export default function AffiliateCard() {
  const { data } = useAffiliate()
  const setStatus = useSetAffiliateStatus()
  const setPromo = useSetAffiliatePromoConsent()
  const [copied, setCopied] = useState(false)
  const [confirmingPromo, setConfirmingPromo] = useState(false)

  if (!data || !data.program_enabled) return null

  const copyLink = async () => {
    if (!data.referral_url) return
    try {
      await navigator.clipboard.writeText(data.referral_url)
      setCopied(true)
      setTimeout(() => setCopied(false), 2500)
      capture(EVENTS.referralLinkCopied)
    } catch {
      setCopied(false)
    }
  }

  // The confirmation line reads the MUTATION result, never the query cache: only the flip response
  // knows whether days actually moved, and the invalidate that follows it replaces the cache with
  // the GET shape (no reward_days, no trial_ends_at) a beat later.
  const flip = setStatus.data
  const trialEnds = flip?.trial_ends_at
    ? new Date(flip.trial_ends_at).toLocaleDateString(undefined, {
        year: 'numeric',
        month: 'short',
        day: 'numeric',
      })
    : null

  return (
    <div className="bg-white rounded-lg shadow-sm border border-gray-200 p-6 space-y-5">
      <div className="flex items-start justify-between gap-4">
        <div>
          <h2 className="text-base font-semibold text-gray-700">Affiliate program</h2>
          <p className="text-xs text-gray-500">
            Share your link and earn extra trial time when someone you refer gets set up and posting.
          </p>
        </div>
        {data.eligible && (
          <Toggle
            on={data.enrolled}
            onClick={() => setStatus.mutate(!data.enrolled)}
          />
        )}
      </div>

      {!data.eligible && (
        <p className="text-sm text-gray-600">
          The affiliate program is currently open to accounts with a LinkedIn company page. Add your
          company page in Setup to join.
        </p>
      )}

      {data.eligible && data.enrolled && (
        <>
          <div className="grid grid-cols-3 gap-3 text-center">
            <div className="rounded-lg bg-gray-50 p-3">
              <p className="text-lg font-bold text-gray-800">{data.referrals.converted}</p>
              <p className="text-xs text-gray-500">Referrals active</p>
            </div>
            <div className="rounded-lg bg-gray-50 p-3">
              <p className="text-lg font-bold text-gray-800">{data.referrals.pending}</p>
              <p className="text-xs text-gray-500">Signed up, not yet active</p>
            </div>
            <div className="rounded-lg bg-gray-50 p-3">
              <p className="text-lg font-bold text-gray-800">
                {data.days_earned}
                <span className="text-xs font-normal text-gray-400"> / {data.max_reward_days}</span>
              </p>
              <p className="text-xs text-gray-500">Bonus trial days</p>
            </div>
          </div>

          <div>
            <label className="block text-sm font-medium text-gray-700 mb-1">Your referral link</label>
            {data.referral_url ? (
              <div className="flex gap-2">
                <input
                  type="text"
                  readOnly
                  value={data.referral_url}
                  data-testid="referral-link"
                  className="flex-1 border border-gray-300 rounded-lg px-3 py-2 text-sm bg-gray-50"
                />
                <button
                  type="button"
                  onClick={copyLink}
                  className="px-4 py-2 rounded-lg bg-blue-600 text-white text-sm font-semibold hover:bg-blue-700"
                >
                  {copied ? 'Copied' : 'Copy'}
                </button>
              </div>
            ) : (
              <p className="text-sm text-gray-500">
                Your link will appear here once signup links are configured for this workspace.
              </p>
            )}
            <p className="mt-1.5 text-xs text-gray-500">
              You get {data.referral_bonus_days} extra trial days each time someone who signs up
              through your link finishes setting up — up to {data.max_reward_days} days in total.
            </p>
          </div>

          {/* (B) — a separate, explicit opt-IN. Never bundled with the toggle above. */}
          <div className="rounded-lg border border-amber-200 bg-amber-50 p-4 space-y-3">
            <div className="flex items-start justify-between gap-4">
              <div>
                <p className="text-sm font-semibold text-amber-900">
                  Let LEM post about LEM from my LinkedIn account
                </p>
                <p className="mt-1 text-xs text-amber-800">
                  Off unless you turn it on. This is your professional account, so we will never
                  publish promotion for us from it without your explicit permission. Any post we
                  write this way carries the required disclosure — you can see the exact wording
                  below — and it still counts against your normal daily posting limits.
                </p>
              </div>
              <Toggle
                on={data.promo_content_opt_in}
                onClick={() => {
                  if (data.promo_content_opt_in) {
                    setPromo.mutate(false)
                    setConfirmingPromo(false)
                  } else {
                    setConfirmingPromo(true)
                  }
                }}
              />
            </div>
            <p className="text-xs text-amber-900 italic">“{data.disclosure_text}”</p>
            {confirmingPromo && !data.promo_content_opt_in && (
              <div className="flex flex-wrap items-center gap-2">
                <button
                  type="button"
                  onClick={() => {
                    setPromo.mutate(true)
                    setConfirmingPromo(false)
                  }}
                  disabled={setPromo.isPending}
                  className="px-3 py-1.5 rounded-md bg-amber-600 text-white text-xs font-semibold hover:bg-amber-700 disabled:opacity-50"
                >
                  I agree — post about LEM from my account
                </button>
                <button
                  type="button"
                  onClick={() => setConfirmingPromo(false)}
                  className="px-3 py-1.5 rounded-md border border-amber-300 text-amber-900 text-xs font-semibold"
                >
                  Cancel
                </button>
              </div>
            )}
            {data.promo_content_opt_in && data.promo_consent_at && (
              <p className="text-xs text-amber-700">
                You agreed on {new Date(data.promo_consent_at).toLocaleDateString()}. Turn this off
                any time — it stops immediately.
              </p>
            )}
          </div>
        </>
      )}

      {data.eligible && !data.enrolled && (
        <p className="text-sm text-gray-600">
          You are not in the affiliate program.{' '}
          {data.bonus_days > 0
            ? `Your trial is the standard ${data.standard_trial_days} days. Join any time to get your referral link back and ${data.bonus_days} bonus trial days.`
            : `Join any time to get your referral link back and earn ${data.referral_bonus_days} extra trial days for each person you refer.`}
        </p>
      )}

      {/* A flip ALWAYS gets a confirmation. `trial_ends_at` is deliberately empty for an account
          that is no longer on a trial (a paid or cancelled user must not be quoted a stale date),
          and a toggle that moves with no acknowledgement at all is the silent half of that fix. */}
      {flip && (
        <p className="text-sm font-medium text-green-600" data-testid="affiliate-trial-note">
          {flip.enrolled
            ? trialEnds
              ? `Joined — your trial runs to ${trialEnds}.`
              : `Joined — your referral link is ready.`
            : !trialEnds
              ? `You've left the program — your subscription is unchanged.`
              : flip.reward_days > 0
                ? `You've left the program — your trial returns to the standard ${flip.standard_trial_days} days, ending ${trialEnds}.`
                : `You've left the program — you keep the trial days you earned, so your trial still ends ${trialEnds}.`}
        </p>
      )}
      {(setStatus.isError || setPromo.isError) && (
        <p className="text-sm font-medium text-red-600">Could not save — please try again.</p>
      )}
    </div>
  )
}
