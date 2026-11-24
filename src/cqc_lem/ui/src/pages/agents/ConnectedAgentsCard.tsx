import SettingsCard from '../../components/SettingsCard'
import type { McpTokenRow } from '../../hooks/useMcpCredentials'

function when(value: string | null): string {
  if (!value) return '—'
  const d = new Date(value)
  return Number.isNaN(d.getTime()) ? '—' : d.toLocaleDateString()
}

function isExpired(row: McpTokenRow, now: Date = new Date()): boolean {
  if (!row.expires_at) return false
  const d = new Date(row.expires_at)
  return !Number.isNaN(d.getTime()) && d <= now
}

function scopeLabel(scope: string): string {
  return scope === 'mcp_outreach' ? 'Content + outreach & leads' : 'Content & analytics'
}

const BUTTON = 'text-xs font-semibold hover:underline disabled:opacity-50 whitespace-nowrap min-h-9'

/**
 * The connections that exist (issue #2687, plan §6 card 5). Token-free by construction: the API
 * returns ids, labels and counts, never a token, so nothing here can leak one. An expired row is
 * greyed but kept — Rotate is how it comes back with the same name and scope.
 */
export default function ConnectedAgentsCard({
  tokens,
  busyId,
  onRotate,
  onRevoke,
}: {
  tokens: McpTokenRow[]
  busyId: number | null
  onRotate: (row: McpTokenRow) => void
  onRevoke: (row: McpTokenRow) => void
}) {
  const allExpired = tokens.length > 0 && tokens.every((t) => isExpired(t))

  return (
    <SettingsCard
      title="Connected agents"
      subtitle="Every token that can reach your account, and when it was last used."
    >
      {tokens.length === 0 ? (
        <p className="text-sm text-fg-muted">No agents connected yet. Create a connection above to add one.</p>
      ) : (
        <>
          {allExpired && (
            <p className="text-sm text-warning" role="status">
              Every connection has expired. Rotate one to get a new paste-ready command.
            </p>
          )}
          <ul className="divide-y divide-hairline border border-hairline rounded-panel">
            {tokens.map((t) => {
              const expired = isExpired(t)
              const busy = busyId === t.session_id
              return (
                <li
                  key={t.session_id}
                  data-testid={`mcp-row-${t.session_id}`}
                  className={`flex flex-col sm:flex-row sm:items-center justify-between gap-2 px-3 py-2 ${
                    expired ? 'opacity-60' : ''}`}
                >
                  <div className="min-w-0">
                    {/* Wraps rather than truncates: on a phone the scope badge is the part that matters. */}
                    <p className="flex flex-wrap items-center gap-x-2 gap-y-1 text-sm font-medium text-fg break-words">
                      <span className="min-w-0 break-words">{t.label}</span>
                      <span className="text-xs font-semibold text-accent bg-accent/5 px-2 py-0.5 rounded-control">
                        {scopeLabel(t.scope)}
                      </span>
                      {expired && (
                        <span className="text-xs font-semibold text-fg-muted bg-page px-2 py-0.5 rounded-control">
                          Expired
                        </span>
                      )}
                    </p>
                    <p className="text-xs text-fg-muted">
                      {t.last_client_name ? `${t.last_client_name} · ` : ''}
                      Created {when(t.created_at)} · Last used {when(t.last_seen_at)} ·{' '}
                      {t.calls_7d === null ? 'calls not counted' : `${t.calls_7d} calls in 7 days`} ·{' '}
                      {expired ? 'Expired' : 'Expires'} {when(t.expires_at)}
                    </p>
                  </div>
                  <div className="flex items-center gap-4 shrink-0">
                    <button
                      type="button"
                      onClick={() => onRotate(t)}
                      disabled={busy}
                      aria-label={`Rotate ${t.label}`}
                      className={`${BUTTON} text-accent`}
                    >
                      Rotate
                    </button>
                    <button
                      type="button"
                      onClick={() => onRevoke(t)}
                      disabled={busy}
                      aria-label={`Revoke ${t.label}`}
                      className={`${BUTTON} text-danger`}
                    >
                      Revoke
                    </button>
                  </div>
                </li>
              )
            })}
          </ul>
        </>
      )}
    </SettingsCard>
  )
}
