import { useState } from 'react'
import TableScroll from '../components/TableScroll'
import { useAuth } from '../contexts/useAuth'
import { useAdminAuditLog } from '../hooks/useAdminAuditLog'

const PAGE_SIZE = 50

// Same labels as the account page's Security card (issue #745, phase 2b/2c) plus the admin-only
// events added in #1450/#1603 — one vocabulary, read by the person it happened to on their own
// page and by an admin here.
const EVENT_LABELS: Record<string, string> = {
  login_success: 'Signed in',
  login_failed: 'Failed sign-in',
  login_rate_limited: 'Sign-in rate limited',
  pin_locked: 'PIN locked',
  logout: 'Signed out',
  session_revoked: 'Device signed out',
  sessions_revoked_all: 'All other devices signed out',
  email_change_requested: 'Email change requested',
  email_changed: 'Email changed',
  factor_added: 'Two-factor method added',
  factor_removed: 'Two-factor method removed',
  step_up_verified: 'Identity confirmed',
  step_up_denied: 'Change blocked — identity not confirmed',
  admin_granted: 'Admin access granted',
  admin_revoked: 'Admin access removed',
  admin_user_disabled: 'Account disabled',
  admin_user_enabled: 'Account re-enabled',
  admin_subscription_granted: 'Subscription extended',
}

function when(value: string | null): string {
  if (!value) return '—'
  const d = new Date(value)
  return Number.isNaN(d.getTime()) ? '—' : d.toLocaleString()
}

function errorText(error: unknown): string | null {
  if (!error) return null
  const detail = (error as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail
  if (typeof detail === 'string') return detail
  return error instanceof Error ? error.message : 'unknown error'
}

/**
 * The admin-facing view over `auth_audit_log` (issue #1603) — who changed whose role or state,
 * when, from where. Read-only: every write it reports was made from `AdminUsersPage`. Never shows
 * `ip_hash` — the API does not return it (stored for forensics, not for a screen).
 */
export default function AdminAuditLogPage() {
  const { sessionToken } = useAuth()
  const [userIdFilter, setUserIdFilter] = useState('')
  const [page, setPage] = useState(0)

  const parsedUserId = userIdFilter.trim() ? Number(userIdFilter.trim()) : undefined
  const { data, isLoading, error, refetch } = useAdminAuditLog({
    sessionToken: sessionToken || '',
    userId: Number.isFinite(parsedUserId) ? parsedUserId : undefined,
    limit: PAGE_SIZE,
    offset: page * PAGE_SIZE,
  })

  const items = data?.items ?? []
  const total = data?.total ?? 0

  return (
    <div className="max-w-5xl space-y-6">
      <div>
        <h1 className="text-2xl font-bold text-gray-800">Audit log</h1>
        <p className="text-sm text-gray-500">
          Every admin role, disable/enable and subscription-grant change — who did it, and to whom.
        </p>
      </div>

      <div className="flex flex-wrap items-center gap-3">
        <input
          type="number"
          aria-label="Filter by user id"
          placeholder="Filter by user id…"
          value={userIdFilter}
          onChange={(e) => { setUserIdFilter(e.target.value); setPage(0) }}
          className="text-sm border border-gray-300 rounded-lg px-3 py-1.5 w-48"
        />
        <button
          onClick={() => void refetch()}
          disabled={isLoading}
          className="text-sm text-blue-600 hover:text-blue-800 font-medium disabled:opacity-50"
        >
          Refresh
        </button>
      </div>

      {error && (
        <p className="text-sm text-red-600">Could not load the audit log: {errorText(error)}</p>
      )}

      <div className="bg-white border border-gray-200 rounded-xl shadow-sm overflow-hidden">
        <TableScroll label="Audit log" minWidth={800} testId="audit-log-table">
          <table className="w-full text-sm text-left">
            <thead className="bg-gray-50 text-gray-600 font-medium">
              <tr>
                <th className="px-4 py-3">When</th>
                <th className="px-4 py-3">Event</th>
                <th className="px-4 py-3">Account</th>
                <th className="px-4 py-3">Result</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-100">
              {items.map((item) => (
                <tr key={item.id} className="hover:bg-gray-50">
                  <td className="px-4 py-3 text-gray-500">{when(item.created_at)}</td>
                  <td className="px-4 py-3">{EVENT_LABELS[item.event] ?? item.event}</td>
                  <td className="px-4 py-3">
                    {item.email ?? (item.user_id !== null ? `user #${item.user_id}` : '—')}
                  </td>
                  <td className="px-4 py-3">
                    {item.success
                      ? <span className="text-gray-600">OK</span>
                      : <span className="text-amber-700">Refused</span>}
                  </td>
                </tr>
              ))}
              {items.length === 0 && !isLoading && (
                <tr>
                  <td colSpan={4} className="px-4 py-8 text-center text-gray-500">
                    No audit rows match the current filter.
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </TableScroll>
      </div>

      <div className="flex items-center justify-between text-sm">
        <button
          onClick={() => setPage((p) => Math.max(0, p - 1))}
          disabled={page === 0 || isLoading}
          className="text-blue-600 hover:text-blue-800 disabled:opacity-50"
        >
          Previous
        </button>
        <span className="text-gray-600">
          {total > 0
            ? `${page * PAGE_SIZE + 1}–${Math.min((page + 1) * PAGE_SIZE, total)} of ${total}`
            : 'No rows'}
        </span>
        <button
          onClick={() => setPage((p) => p + 1)}
          disabled={isLoading || (page + 1) * PAGE_SIZE >= total}
          className="text-blue-600 hover:text-blue-800 disabled:opacity-50"
        >
          Next
        </button>
      </div>
    </div>
  )
}
