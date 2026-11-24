import { useQuery } from '@tanstack/react-query'
import api from '../../api/client'
import type { ApiEnvelope } from '../../api/types'
import SettingsCard from '../../components/SettingsCard'
import { useAuth } from '../../contexts/useAuth'
import { MCP_ACTIVITY_KEY, describe } from './agentActivity'
import type { McpActivityEvent } from './agentActivity'

// What the user's agents did (MCP W2, plan §3.5): `GET /user/mcp-token/activity`, the account's
// latest `mcp_*` audit rows. Token-free and body-free by construction: a row carries an event name,
// the tool and the id it touched, never a message or a token.

function when(value: string | null): string {
  if (!value) return '—'
  const d = new Date(value)
  return Number.isNaN(d.getTime()) ? '—' : d.toLocaleString()
}

export default function AgentActivityCard() {
  const { sessionToken } = useAuth()
  const activity = useQuery({
    queryKey: [MCP_ACTIVITY_KEY, sessionToken],
    queryFn: () =>
      api
        .get<ApiEnvelope<{ events: McpActivityEvent[] }>>(
          `/user/mcp-token/activity?session_token=${encodeURIComponent(sessionToken!)}`)
        .then((r) => r.data.detail.events),
    enabled: !!sessionToken,
    staleTime: 30 * 1000,
    retry: 1,
  })

  return (
    <SettingsCard
      title="Agent activity"
      subtitle="What your agents did, newest first. Anything they create waits for your approval."
    >
      {activity.isLoading ? (
        <div className="h-16" aria-busy="true" />
      ) : activity.isError ? (
        <div role="alert" className="space-y-1">
          <p className="text-sm text-fg">Could not load agent activity.</p>
          <button
            type="button"
            onClick={() => activity.refetch()}
            className="text-sm font-semibold text-accent hover:underline"
          >
            Try again
          </button>
        </div>
      ) : !activity.data?.length ? (
        <p className="text-sm text-fg-muted">No agent activity yet.</p>
      ) : (
        <ul className="divide-y divide-hairline border border-hairline rounded-panel">
          {activity.data.map((e, n) => (
            <li key={`${e.created_at}-${n}`} data-testid="mcp-activity-row" className="px-3 py-2">
              <p className="text-sm text-fg break-words">{describe(e)}</p>
              <p className="text-xs text-fg-muted">
                {when(e.created_at)}
                {e.client ? ` · ${e.client}` : ''}
              </p>
            </li>
          ))}
        </ul>
      )}
    </SettingsCard>
  )
}
