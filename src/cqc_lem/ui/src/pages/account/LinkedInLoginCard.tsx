import { useState } from 'react'
import { useMutation, useQuery } from '@tanstack/react-query'
import api from '../../api/client'
import { useAuth } from '../../contexts/AuthContext'
import { useAccountReadiness } from '../../hooks/useAccountReadiness'
import LinkedInSessionCard from '../../components/LinkedInSessionCard'

const LI_AUTH_URL = '/api/auth/linkedin/'

export default function LinkedInLoginCard() {
  const { user, sessionToken } = useAuth()
  const email = user?.email ?? ''
  const { data: readiness } = useAccountReadiness()
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
          is_expiring_soon: boolean
          is_expired: boolean
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

  const showLinkedInSection = !isLinkedInConnected || tokenExpiringSoon || tokenExpired

  // LinkedIn password save
  const liPasswordMutation = useMutation({
    mutationFn: () =>
      api.put('/user/linkedin-password', {
        session_token: sessionToken,
        linkedin_password: liPassword,
      }),
    onSuccess: () => {
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
      {/* LinkedIn connection — hidden when connected and token healthy */}
      {showLinkedInSection && (
        <div className="bg-white rounded-lg shadow-sm border border-gray-200 p-6 space-y-4">
          <h2 className="text-base font-semibold text-gray-700">
            LinkedIn Connection <span className="text-red-500">*</span>
            <span className="ml-2 text-[11px] font-semibold text-red-600 bg-red-50 px-2 py-0.5 rounded">
              Required
            </span>
          </h2>

          {isLinkedInConnected && tokenExpiringSoon && !tokenExpired && (
            <div className="bg-yellow-50 border border-yellow-200 rounded-lg p-3 text-sm text-yellow-800">
              Your LinkedIn token is expiring soon. Please reconnect to keep posting.
              {tokenStatusData?.refresh_attempted && !tokenStatusData?.refresh_succeeded && (
                <span className="ml-1 text-xs">(Auto-refresh failed — manual reconnect required.)</span>
              )}
            </div>
          )}

          {tokenExpired && isLinkedInConnected && (
            <div className="bg-red-50 border border-red-200 rounded-lg p-3 text-sm text-red-800">
              Your LinkedIn connection has expired. Please reconnect to continue posting.
            </div>
          )}

          <div className="flex items-center gap-3">
            <div
              className={`w-3 h-3 rounded-full flex-shrink-0 ${
                isLinkedInConnected && !tokenExpired ? 'bg-green-500' : 'bg-gray-300'
              }`}
            />
            <span className="text-sm text-gray-600">
              {isLinkedInConnected && !tokenExpired ? 'Connected to LinkedIn' : 'Not connected'}
            </span>
          </div>

          <p className="text-xs text-gray-500">
            Connecting allows LEM to post on your behalf, send DMs, reply to comments, and engage with your network automatically.
          </p>

          <a
            href={`${LI_AUTH_URL}?email=${encodeURIComponent(email)}&session_token=${encodeURIComponent(sessionToken ?? '')}`}
            className="inline-block w-full text-center py-2 rounded-lg text-sm font-semibold bg-blue-700 text-white hover:bg-blue-800 cursor-pointer transition-colors"
          >
            {isLinkedInConnected ? 'Reconnect LinkedIn' : 'Connect LinkedIn'}
          </a>
        </div>
      )}

      {/* LinkedIn Session (cookie) — the DEFAULT engagement login (issue #745, decision 2A) */}
      <LinkedInSessionCard connected={sessionOk} migrationNeeded={cookieMigrationNeeded} />

      {/* LinkedIn Automation Password — DEPRECATED, collapsed behind a disclosure so the cookie
          path above is the one users take. Kept working for accounts that already rely on it. */}
      {isLinkedInConnected && !tokenExpired && (
        <details className="bg-white rounded-lg shadow-sm border border-gray-200 p-6 space-y-4">
          <summary className="cursor-pointer text-sm font-medium text-gray-500">
            Use a LinkedIn password instead (not recommended)
          </summary>
          <div className="pt-4">
            <h2 className="text-base font-semibold text-gray-700">
              LinkedIn Automation Password{' '}
              <span className="ml-1 text-[11px] font-semibold text-amber-700 bg-amber-50 px-2 py-0.5 rounded">
                Deprecated
              </span>
            </h2>
            <p className="text-xs text-gray-500 mt-1">
              Browser automation types this password into LinkedIn's login form, so it has to be
              stored in a reversible form — encrypted at rest, but still recoverable by the app.
              A session cookie above does the same job, is revocable from LinkedIn, and is not a
              credential you reuse anywhere else. Prefer it.
            </p>
          </div>

          <div>
            <label className="block text-sm font-medium text-gray-700 mb-1">LinkedIn Password</label>
            <div className="relative">
              <input
                type={showLiPassword ? 'text' : 'password'}
                value={liPassword}
                onChange={(e) => setLiPassword(e.target.value)}
                className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm pr-16 focus:outline-none focus:ring-2 focus:ring-blue-500"
                placeholder="Enter your LinkedIn password"
                autoComplete="new-password"
              />
              <button
                type="button"
                onClick={() => setShowLiPassword((v) => !v)}
                className="absolute inset-y-0 right-2 flex items-center px-2 text-xs text-gray-500 hover:text-gray-700"
              >
                {showLiPassword ? 'Hide' : 'Show'}
              </button>
            </div>
          </div>

          {liPasswordMsg && (
            <p className={`text-sm font-medium ${liPasswordMsg.ok ? 'text-green-600' : 'text-red-600'}`}>
              {liPasswordMsg.text}
            </p>
          )}

          <button
            type="button"
            onClick={() => liPasswordMutation.mutate()}
            disabled={liPasswordMutation.isPending || !liPassword}
            className="w-full bg-blue-600 text-white py-2 rounded-lg text-sm font-semibold hover:bg-blue-700 disabled:opacity-50 transition-colors"
          >
            {liPasswordMutation.isPending ? 'Saving…' : 'Save Password'}
          </button>
        </details>
      )}
    </>
  )
}
