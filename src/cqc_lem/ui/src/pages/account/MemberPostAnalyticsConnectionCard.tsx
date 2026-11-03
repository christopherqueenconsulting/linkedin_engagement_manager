import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import api from '../../api/client'
import { useAuth } from '../../contexts/useAuth'

type AnalyticsConnection = {
  available: boolean
  connect_available: boolean
  analytics_enabled: boolean
  separate_app: boolean
  connected: boolean
  saved_token: boolean
  expired: boolean
  expires_at: string | null
}

export default function MemberPostAnalyticsConnectionCard() {
  const { sessionToken } = useAuth()
  const queryClient = useQueryClient()
  const { data } = useQuery({
    queryKey: ['member-post-analytics-connection', sessionToken],
    queryFn: () =>
      api
        .get(`/user/member-post-analytics-connection?session_token=${encodeURIComponent(sessionToken!)}`)
        .then((r) => r.data.detail as AnalyticsConnection),
    enabled: !!sessionToken,
    staleTime: 60 * 1000,
  })

  const disconnect = useMutation({
    mutationFn: () =>
      api.delete(`/user/member-post-analytics-connection?session_token=${encodeURIComponent(sessionToken!)}`),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['member-post-analytics-connection'] }),
  })

  if (!data?.available) return null
  const expiry = data.expires_at
    ? new Date(data.expires_at).toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' })
    : null

  return (
    <section className="bg-raised rounded-panel shadow-panel border border-hairline p-6 space-y-3">
      <h2 className="text-base font-semibold text-fg">Community analytics test connection</h2>
      <p className="text-xs text-fg-muted">
        Temporary LI Manage v2 connection for post analytics. It uses a separate encrypted token;
        your existing LinkedIn connection still handles publishing and other LEM actions. Connect
        the same LinkedIn profile used by LEM.
      </p>
      <p className="text-sm text-fg-muted">
        {data.connected
          ? `Connected${expiry ? ` · expires ${expiry}` : ''}`
          : data.expired
            ? 'Connection expired. Reconnect to resume analytics reads.'
            : 'Not connected'}
        {data.connected && !data.analytics_enabled ? ' · analytics reads are currently off' : ''}
        {data.connected && !data.separate_app ? ' · saved, but main LEM app handles analytics reads' : ''}
      </p>
      <div className="flex flex-wrap gap-3">
        {data.connect_available && (
          <a
            href={`/api/auth/linkedin/analytics/?session_token=${encodeURIComponent(sessionToken ?? '')}`}
            className="inline-block text-center px-4 py-2 rounded-control text-sm font-semibold bg-accent text-white hover:bg-accent/90"
          >
            {data.connected ? 'Reconnect analytics app' : 'Connect analytics app'}
          </a>
        )}
        {data.saved_token && (
          <button
            type="button"
            onClick={() => disconnect.mutate()}
            disabled={disconnect.isPending}
            className="px-4 py-2 rounded-control text-sm font-semibold border border-hairline text-fg-muted hover:text-fg disabled:opacity-50"
          >
            {disconnect.isPending ? 'Disconnecting…' : 'Disconnect analytics app'}
          </button>
        )}
      </div>
      {disconnect.isError && <p className="text-xs text-danger">Could not remove analytics connection.</p>}
      {disconnect.isSuccess && <p className="text-xs text-success">Analytics connection removed.</p>}
    </section>
  )
}
