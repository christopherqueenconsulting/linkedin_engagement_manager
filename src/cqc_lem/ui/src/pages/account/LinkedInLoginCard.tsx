import { useState } from 'react'
import { useMutation, useQuery } from '@tanstack/react-query'
import api from '../../api/client'
import { useAuth } from '../../contexts/useAuth'
import { useAccountReadiness } from '../../hooks/useAccountReadiness'
import { useSuggestOnly } from '../../hooks/useEngagementMode'
import LinkedInSessionCard from '../../components/LinkedInSessionCard'
import LinkedInSignInStatusCard from './LinkedInSignInStatusCard'
import MemberPostAnalyticsConnectionCard from './MemberPostAnalyticsConnectionCard'
import { useStepUp } from '../../hooks/useStepUp'

const LI_AUTH_URL = '/api/auth/linkedin/'

export default function LinkedInLoginCard() {
  const { sessionToken } = useAuth()
  const { guard, stepUpModal } = useStepUp()
  const { data: readiness } = useAccountReadiness()
  const suggestOnly = useSuggestOnly()
  const sessionOk = readiness?.items.find((i) => i.key === 'linkedin_session')?.ok ?? false
  const cookieMigrationNeeded = readiness?.cookie_migration_needed ?? false

  const [liConnectedLocal] = useState(localStorage.getItem('lem_li_connected') === '1')
  const [liPassword, setLiPassword] = useState('')
  const [showLiPassword, setShowLiPassword] = useState(false)
  const [liPasswordMsg, setLiPasswordMsg] = useState<{ ok: boolean; text: string } | null>(null)

  // LinkedIn token status
  const { data: tokenStatusData } = useQuery({
    queryKey: ['token-status', sessionToken],
    queryFn: () =>
      api
        .get(`/user/token_status?session_token=${encodeURIComponent(sessionToken!)}`)
        .then((r) => r.data.detail as {
          token_expiry_date: string | null
          days_remaining: number | null
          is_expiring_soon: boolean
          is_expired: boolean
          can_auto_refresh: boolean
          refresh_attempted: boolean
          refresh_succeeded: boolean
        }),
    enabled: !!sessionToken,
    staleTime: 5 * 60 * 1000,
  })

  const hasValidToken = tokenStatusData ? !tokenStatusData.is_expired : false
  const isLinkedInConnected = liConnectedLocal || hasValidToken
  const tokenExpiringSoon = tokenStatusData?.is_expiring_soon ?? false
  const tokenExpired = tokenStatusData?.is_expired ?? false
  const daysRemaining = tokenStatusData?.days_remaining ?? null
  const canAutoRefresh = tokenStatusData?.can_auto_refresh ?? false
  const expiryDate = tokenStatusData?.token_expiry_date
    ? new Date(tokenStatusData.token_expiry_date).toLocaleDateString(undefined, {
        month: 'short',
        day: 'numeric',
        year: 'numeric',
      })
    : null

  // "N days left" is the thing the user actually asked to see (issue #600). null days_remaining
  // means we could not read an expiry — say so rather than render an alarming "0 days".
  const countdownText =
    daysRemaining === null
      ? 'Expiration date unknown'
      : daysRemaining <= 0
        ? 'Expired'
        : `${daysRemaining} ${daysRemaining === 1 ? 'day' : 'days'} left${
            expiryDate ? ` — expires ${expiryDate}` : ''
          }`

  const showLinkedInSection = !isLinkedInConnected || tokenExpiringSoon || tokenExpired
  // Healthy connections used to render nothing at all, so there was no way to see how much time
  // was left until the warning fired. Show a compact countdown strip instead — always, not only
  // inside the warning window (issue #600, owner decision 2A).
  const showHealthyCountdown = !showLinkedInSection && isLinkedInConnected && !!tokenStatusData

  // LinkedIn password save — step-up gated since 2c: a stored LinkedIn password is the worst
  // single item in the database (design §1), so writing one has to cost a proven factor.
  const liPasswordMutation = useMutation({
    mutationFn: () =>
      guard(() =>
        api.put('/user/linkedin-password', {
          session_token: sessionToken,
          linkedin_password: liPassword,
        }),
      ),
    onSuccess: (result) => {
      if (result === null) return
      setLiPassword('')
      setLiPasswordMsg({ ok: true, text: 'LinkedIn password saved.' })
      setTimeout(() => setLiPasswordMsg(null), 4000)
    },
    onError: () => {
      setLiPasswordMsg({ ok: false, text: 'Save failed — please try again.' })
      setTimeout(() => setLiPasswordMsg(null), 5000)
    },
  })

  return (
    <>
      {stepUpModal}
      {/* LinkedIn connection — hidden when connected and token healthy */}
      {showLinkedInSection && (
        <div className="bg-raised rounded-panel shadow-panel border border-hairline p-6 space-y-4">
          <h2 className="text-base font-semibold text-fg">
            LinkedIn Connection <span className="text-danger">*</span>
            <span className="ml-2 text-xs font-semibold text-danger bg-danger/5 px-2 py-0.5 rounded-control">
              Required
            </span>
          </h2>

          {isLinkedInConnected && tokenExpiringSoon && !tokenExpired && (
            <div className="bg-warning/5 border border-warning/25 rounded-panel p-3 text-sm text-warning">
              Your LinkedIn authorization expires in{' '}
              <strong>
                {daysRemaining === null
                  ? 'less than 30 days'
                  : `${daysRemaining} ${daysRemaining === 1 ? 'day' : 'days'}`}
              </strong>
              {expiryDate ? ` (on ${expiryDate})` : ''}. Please reconnect to keep posting.
              <span className="block mt-1 text-xs">
                {tokenStatusData?.refresh_attempted && !tokenStatusData?.refresh_succeeded
                  ? 'We tried to renew it automatically and could not — a manual reconnect is required.'
                  : canAutoRefresh
                    ? 'We retry the automatic renewal every day, and email you if it keeps failing.'
                    : 'LinkedIn does not let us renew this authorization for you — reconnecting restarts the 60-day clock.'}
              </span>
            </div>
          )}

          {tokenExpired && isLinkedInConnected && (
            <div className="bg-danger/5 border border-danger/25 rounded-panel p-3 text-sm text-danger">
              Your LinkedIn connection has expired. Please reconnect to continue posting.
            </div>
          )}

          <div className="flex items-center gap-3">
            <div
              className={`w-3 h-3 rounded-full flex-shrink-0 ${
                isLinkedInConnected && !tokenExpired ? 'bg-success' : 'bg-hairline'
              }`}
            />
            <span className="text-sm text-fg-muted">
              {isLinkedInConnected && !tokenExpired ? 'Connected to LinkedIn' : 'Not connected'}
            </span>
          </div>

          <p className="text-xs text-fg-muted" data-testid="linkedin-connect-purpose">
            {suggestOnly
              ? "Connecting lets LEM publish the posts you approve through LinkedIn's API. Your account is in suggestion mode, so LEM writes comments and messages for you to send yourself and does not comment, reply or message for you."
              : 'Connecting allows LEM to post on your behalf, send DMs, reply to comments, and engage with your network automatically.'}
          </p>

          <a
            href={`${LI_AUTH_URL}?session_token=${encodeURIComponent(sessionToken ?? '')}`}
            className="inline-block w-full text-center py-2 rounded-panel text-sm font-semibold bg-accent text-white hover:bg-accent/90 cursor-pointer transition-colors"
          >
            {isLinkedInConnected ? 'Reconnect LinkedIn' : 'Connect LinkedIn'}
          </a>
        </div>
      )}

      {showHealthyCountdown && (
        <div className="bg-raised rounded-panel shadow-panel border border-hairline px-6 py-4 flex items-center justify-between gap-3">
          <div className="flex items-center gap-3 min-w-0">
            <div className="w-3 h-3 rounded-full flex-shrink-0 bg-success" />
            <span className="text-sm text-fg-muted truncate">
              Connected to LinkedIn ·{' '}
              <span className="text-fg-muted">{countdownText}</span>
            </span>
          </div>
          <a
            href={`${LI_AUTH_URL}?session_token=${encodeURIComponent(sessionToken ?? '')}`}
            className="text-xs font-semibold text-accent hover:text-accent whitespace-nowrap"
          >
            Reconnect
          </a>
        </div>
      )}

      {/* LinkedIn Session (cookie) — the DEFAULT engagement login (issue #745, decision 2A) */}
      <LinkedInSessionCard connected={sessionOk} migrationNeeded={cookieMigrationNeeded} />

      {/* Whether the automation's last sign-in landed, and whether it is waiting on the device
          approval the user was emailed about (issue #933). A saved session says only that a
          credential exists — this says whether it worked. */}
      <LinkedInSignInStatusCard />

      <MemberPostAnalyticsConnectionCard />

      {/* LinkedIn Automation Password — DEPRECATED, collapsed behind a disclosure so the cookie
          path above is the one users take. Kept working for accounts that already rely on it. */}
      {isLinkedInConnected && !tokenExpired && (
        <details className="bg-raised rounded-panel shadow-panel border border-hairline p-6 space-y-4">
          <summary className="cursor-pointer text-sm font-medium text-fg-muted">
            Use a LinkedIn password instead (not recommended)
          </summary>
          <div className="pt-4">
            <h2 className="text-base font-semibold text-fg">
              LinkedIn Automation Password{' '}
              <span className="ml-1 text-xs font-semibold text-warning bg-warning/5 px-2 py-0.5 rounded-control">
                Deprecated
              </span>
            </h2>
            <p className="text-xs text-fg-muted mt-1">
              Browser automation types this password into LinkedIn's login form, so it has to be
              stored in a reversible form — encrypted at rest, but still recoverable by the app.
              A session cookie above does the same job, is revocable from LinkedIn, and is not a
              credential you reuse anywhere else. Prefer it.
            </p>
          </div>

          <div>
            <label className="block text-sm font-medium text-fg mb-1">LinkedIn Password</label>
            <div className="relative">
              <input
                type={showLiPassword ? 'text' : 'password'}
                value={liPassword}
                onChange={(e) => setLiPassword(e.target.value)}
                className="w-full border border-field rounded-control px-3 py-2 text-sm pr-16 focus:outline-none focus:ring-2 focus:ring-accent"
                placeholder="Enter your LinkedIn password"
                autoComplete="new-password"
              />
              <button
                type="button"
                onClick={() => setShowLiPassword((v) => !v)}
                className="absolute inset-y-0 right-2 flex items-center px-2 text-xs text-fg-muted hover:text-fg"
              >
                {showLiPassword ? 'Hide' : 'Show'}
              </button>
            </div>
          </div>

          {liPasswordMsg && (
            <p className={`text-sm font-medium ${liPasswordMsg.ok ? 'text-success' : 'text-danger'}`}>
              {liPasswordMsg.text}
            </p>
          )}

          <button
            type="button"
            onClick={() => liPasswordMutation.mutate()}
            disabled={liPasswordMutation.isPending || !liPassword}
            className="w-full bg-accent text-white py-2 rounded-panel text-sm font-semibold hover:bg-accent/90 disabled:opacity-50 transition-colors"
          >
            {liPasswordMutation.isPending ? 'Saving…' : 'Save Password'}
          </button>
        </details>
      )}
    </>
  )
}
