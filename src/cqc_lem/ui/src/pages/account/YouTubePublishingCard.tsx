import { useRef } from 'react'
import { useQuery } from '@tanstack/react-query'
import api from '../../api/client'
import { useAuth } from '../../contexts/AuthContext'
import SettingsCard from '../../components/SettingsCard'

type YouTubeStatus = {
  configured: boolean
  connected: boolean
  status: 'ok' | 'needs_reauth' | 'unknown' | 'not_configured'
  reason?: string | null
  error?: string | null
  scope?: string | null
  checked_at?: string | null
  token_source?: string | null
  token_updated_at?: string | null
  privacy_status?: string | null
  runbook?: string | null
}

/**
 * YouTube publishing state for the tutorial pipeline (issue #742), owner-visible without reading
 * logs. Admin-only, and silent on an install that has no YouTube credentials at all — publishing
 * is off by design until ~1.0 and an empty "not connected" card would read as a fault.
 *
 * The four states are NOT two: `unknown` (Google unreachable) is deliberately distinct from
 * `needs_reauth` (the grant is provably gone), because only the second is something to act on.
 */
export default function YouTubePublishingCard() {
  const { sessionToken, isAdmin } = useAuth()
  // One-shot, deliberately NOT query state: a `live` flag in the key latches on after the first
  // click, so every window refocus past staleTime would then re-probe Google — the opposite of the
  // "opening Settings never spends a round trip" contract. The ref arms exactly one live fetch.
  const liveOnce = useRef(false)

  const { data, isFetching, refetch } = useQuery({
    queryKey: ['youtube-status', sessionToken],
    queryFn: () => {
      const live = liveOnce.current
      liveOnce.current = false
      return api
        .get(
          `/admin/youtube-status?session_token=${encodeURIComponent(sessionToken!)}&live=${live}`,
        )
        .then((r) => r.data.detail as YouTubeStatus)
    },
    enabled: !!sessionToken && isAdmin,
    staleTime: 60 * 1000,
  })

  if (!isAdmin || !data || !data.configured) return null

  const badge =
    data.status === 'ok'
      ? { text: 'Connected', className: 'text-green-700 bg-green-50' }
      : data.status === 'needs_reauth'
        ? { text: 'Needs re-auth', className: 'text-red-700 bg-red-50' }
        : { text: 'Unknown', className: 'text-amber-700 bg-amber-50' }

  return (
    <SettingsCard
      title="YouTube Publishing"
      subtitle="Where the automated feature tutorials are published. Checked every week — the check is also what keeps the token alive while the feature is off."
      headerRight={
        <span
          data-testid="youtube-status-badge"
          className={`text-[11px] font-semibold px-2 py-0.5 rounded whitespace-nowrap ${badge.className}`}
        >
          {badge.text}
        </span>
      }
    >
      {data.reason && <p className="text-xs text-gray-600">{data.reason}</p>}

      {data.status === 'needs_reauth' && (
        <p className="text-xs text-red-600">
          Tutorial runs abort before spending on narration or rendering until this is re-minted.
          Re-mint the refresh token (see <code>{data.runbook ?? 'docs/youtube-publishing.md'}</code>)
          and install it with <code>POST /api/admin/youtube-token</code> — no deploy needed.
        </p>
      )}

      <dl className="text-xs text-gray-500 space-y-1">
        <div className="flex gap-2">
          <dt className="font-medium text-gray-600">Last checked</dt>
          <dd>{data.checked_at ?? 'never'}</dd>
        </div>
        <div className="flex gap-2">
          <dt className="font-medium text-gray-600">Token stored in</dt>
          <dd>{data.token_source === 'db' ? 'database (rotatable without a deploy)' : '.env'}</dd>
        </div>
        {data.privacy_status && (
          <div className="flex gap-2">
            <dt className="font-medium text-gray-600">Uploads are</dt>
            <dd>{data.privacy_status}</dd>
          </div>
        )}
      </dl>

      <button
        type="button"
        onClick={() => {
          liveOnce.current = true
          refetch()
        }}
        disabled={isFetching}
        className="w-full bg-blue-600 text-white py-2 rounded-lg text-sm font-semibold hover:bg-blue-700 disabled:opacity-50 transition-colors"
      >
        {isFetching ? 'Checking…' : 'Check now'}
      </button>
    </SettingsCard>
  )
}
