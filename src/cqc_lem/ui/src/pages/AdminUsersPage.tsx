import { Fragment, useEffect, useState } from 'react'
import TableScroll from '../components/TableScroll'
import { useAuth } from '../contexts/useAuth'
import {
  useAdminUserDetail,
  useAdminUsers,
  useGrantSubscription,
  useSetUserAdmin,
  useSetUserDisabled,
} from '../hooks/useAdminUsers'
import type { AdminUserSummary } from '../hooks/useAdminUsers'

const PAGE_SIZE = 25

const SUBSCRIPTION_LABELS: Record<string, string> = {
  active: 'Active',
  inactive: 'Inactive',
  trial: 'Trial',
  cancelled: 'Cancelled',
  past_due: 'Past due',
}

const CONNECTION_LABELS: Record<string, string> = {
  connected: 'Connected',
  expired: 'Expired',
  disconnected: 'Disconnected',
}

function classNames(...classes: (string | false | null | undefined)[]) {
  return classes.filter(Boolean).join(' ')
}

function when(value: string | null | undefined): string {
  return value ? new Date(value).toLocaleDateString() : '—'
}

function whenExact(value: string | null | undefined): string {
  return value ? new Date(value).toLocaleString() : '—'
}

function errorText(error: unknown): string | null {
  if (!error) return null
  const detail = (error as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail
  if (typeof detail === 'string') return detail
  if (detail && typeof detail === 'object' && 'message' in detail) {
    return String((detail as { message: unknown }).message)
  }
  return error instanceof Error ? error.message : 'unknown error'
}

function DetailRow({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div className="flex justify-between gap-4 py-1 border-b border-gray-100 last:border-0">
      <dt className="text-gray-500">{label}</dt>
      <dd className="text-gray-800 text-right break-all">{value}</dd>
    </div>
  )
}

function SubscriptionGrantForm({ userId, sessionToken }: { userId: number; sessionToken: string }) {
  const [days, setDays] = useState('30')
  const grant = useGrantSubscription()
  const days_n = Number(days)
  const valid = Number.isInteger(days_n) && days_n >= 1 && days_n <= 365

  return (
    <div className="flex flex-col gap-1 pt-2 border-t border-gray-100">
      <div className="flex items-center gap-2">
        <label htmlFor={`grant-days-${userId}`} className="text-gray-500">
          Grant subscription days (one-time — not a standing override)
        </label>
        <input
          id={`grant-days-${userId}`}
          type="number"
          min={1}
          max={365}
          value={days}
          onChange={(e) => setDays(e.target.value)}
          className="w-20 text-sm border border-gray-300 rounded px-2 py-1"
        />
        <button
          onClick={() => valid && grant.mutate({ userId, days: days_n, sessionToken })}
          disabled={!valid || grant.isPending}
          className="text-xs bg-blue-600 text-white px-2.5 py-1 rounded hover:bg-blue-700 disabled:opacity-50"
        >
          {grant.isPending ? 'Working…' : 'Grant'}
        </button>
      </div>
      {grant.isSuccess && (
        <p className="text-xs text-gray-500">
          Extended {grant.data.days_granted} day(s) — new period end: {when(grant.data.subscription_current_period_end)}
        </p>
      )}
      {grant.isError && (
        <p className="text-xs text-amber-700">Could not grant: {errorText(grant.error)}</p>
      )}
    </div>
  )
}

function UserDetail({ userId, sessionToken }: { userId: number; sessionToken: string }) {
  const { data, isLoading, error } = useAdminUserDetail(userId, sessionToken)
  if (isLoading) return <p className="text-sm text-gray-500">Loading account…</p>
  if (error) return <p className="text-sm text-red-600">Could not load: {errorText(error)}</p>
  if (!data) return null
  return (
    <div className="space-y-3">
      <dl className="text-sm grid gap-x-8 md:grid-cols-2">
        <DetailRow label="Public ID" value={data.public_uid ?? '—'} />
        <DetailRow label="Email verified" value={whenExact(data.email_verified_at)} />
        <DetailRow label="LinkedIn name" value={data.linkedin_display_name ?? '—'} />
        <DetailRow label="LinkedIn email" value={data.linkedin_email ?? '—'} />
        <DetailRow label="Trial started" value={when(data.trial_started_at)} />
        <DetailRow label="Billing period ends" value={when(data.subscription_current_period_end)} />
        <DetailRow label="Timezone" value={data.timezone ?? '—'} />
        <DetailRow
          label="Location"
          value={[data.city, data.country].filter(Boolean).join(', ') || '—'}
        />
        <DetailRow label="Content language" value={data.content_language ?? data.locale ?? '—'} />
        <DetailRow label="Blog" value={data.blog_url ?? '—'} />
        <DetailRow label="Company page" value={data.company_linked_in_url ?? '—'} />
        <DetailRow label="Auto-schedule posts" value={data.auto_schedule_posts ? 'On' : 'Off'} />
        <DetailRow label="LinkedIn connected" value={when(data.linkedin_connected_at)} />
        <DetailRow label="Voice set" value={when(data.voice_set_at)} />
        <DetailRow label="First post approved" value={when(data.first_post_approved_at)} />
        <DetailRow label="Activated" value={when(data.activated_at)} />
        {/* Read-only on purpose: an admin editing another person's caps or voice changes what
            LinkedIn sees as that person's writing, with no consent trail. */}
        <DetailRow label="Comments/day" value={data.max_comments_per_day ?? '—'} />
        <DetailRow label="DMs/day" value={data.max_dms_per_day ?? '—'} />
        <DetailRow label="Posts/week" value={data.posts_per_week ?? '—'} />
        <DetailRow label="Comment length" value={data.comment_length ?? '—'} />
        <DetailRow label="Avatar images" value={data.avatar_disabled ? 'Disabled' : 'Enabled'} />
        <DetailRow label="Account status" value={data.disabled ? `Disabled ${whenExact(data.disabled_at)}` : 'Enabled'} />
        <DetailRow label="Last updated" value={whenExact(data.updated_at)} />
      </dl>
      <SubscriptionGrantForm userId={userId} sessionToken={sessionToken} />
    </div>
  )
}

export default function AdminUsersPage() {
  const { sessionToken, user } = useAuth()
  const [search, setSearch] = useState('')
  // The applied term, debounced out of the raw input like ContentStudio's search: `q` becomes an
  // unindexable `LIKE '%…%'` over `users`, so a query per keystroke is a table scan per keystroke.
  const [appliedSearch, setAppliedSearch] = useState('')
  const [subscriptionFilter, setSubscriptionFilter] = useState('')
  const [connectionFilter, setConnectionFilter] = useState('')
  const [adminOnly, setAdminOnly] = useState(false)
  const [page, setPage] = useState(0)
  const [openId, setOpenId] = useState<number | null>(null)

  useEffect(() => {
    const t = setTimeout(() => setAppliedSearch(search.trim()), 400)
    return () => clearTimeout(t)
  }, [search])

  const token = sessionToken || ''
  const { data, isLoading, error, refetch } = useAdminUsers({
    sessionToken: token,
    q: appliedSearch || undefined,
    subscriptionStatus: subscriptionFilter || undefined,
    connectionStatus: connectionFilter || undefined,
    isAdmin: adminOnly ? true : undefined,
    limit: PAGE_SIZE,
    offset: page * PAGE_SIZE,
  })

  const role = useSetUserAdmin()
  const disabling = useSetUserDisabled()
  // Which row the last click belonged to, so a refusal is shown AT the button — the guards
  // (self-revoke, last admin, allowlist admin, self-disable) all leave the table looking the same.
  const [actedId, setActedId] = useState<number | null>(null)
  const [lastAction, setLastAction] = useState<'role' | 'disabled' | null>(null)

  const items = data?.items ?? []
  const total = data?.total ?? 0
  const roleError = errorText(role.error)
  const disabledError = errorText(disabling.error)

  function handleRole(target: AdminUserSummary) {
    setActedId(target.id)
    setLastAction('role')
    role.mutate({ userId: target.id, isAdmin: !target.is_admin, sessionToken: token })
  }

  function handleDisable(target: AdminUserSummary) {
    setActedId(target.id)
    setLastAction('disabled')
    disabling.mutate({ userId: target.id, disabled: !target.disabled, sessionToken: token })
  }

  return (
    <div className="max-w-6xl space-y-6">
      <div>
        <h1 className="text-2xl font-bold text-gray-800">User management</h1>
        <p className="text-sm text-gray-500">
          Every account, who can administer LEM, and whether their automation runs. Subscription
          state comes from billing (a grant is a one-time comp, not an override), and preferences
          belong to their owner.
        </p>
      </div>

      <div className="flex flex-wrap items-center gap-3">
        <input
          type="search"
          aria-label="Search by email"
          placeholder="Search email…"
          value={search}
          onChange={(e) => { setSearch(e.target.value); setPage(0) }}
          className="text-sm border border-gray-300 rounded-lg px-3 py-1.5 w-64"
        />
        <div className="flex items-center gap-2">
          <label htmlFor="subscription-filter" className="text-sm text-gray-600">Subscription</label>
          <select
            id="subscription-filter"
            value={subscriptionFilter}
            onChange={(e) => { setSubscriptionFilter(e.target.value); setPage(0) }}
            className="text-sm border border-gray-300 rounded-lg px-2 py-1"
          >
            <option value="">All</option>
            {Object.entries(SUBSCRIPTION_LABELS).map(([value, label]) => (
              <option key={value} value={value}>{label}</option>
            ))}
          </select>
        </div>
        <div className="flex items-center gap-2">
          <label htmlFor="connection-filter" className="text-sm text-gray-600">LinkedIn</label>
          <select
            id="connection-filter"
            value={connectionFilter}
            onChange={(e) => { setConnectionFilter(e.target.value); setPage(0) }}
            className="text-sm border border-gray-300 rounded-lg px-2 py-1"
          >
            <option value="">All</option>
            {Object.entries(CONNECTION_LABELS).map(([value, label]) => (
              <option key={value} value={value}>{label}</option>
            ))}
          </select>
        </div>
        <label className="flex items-center gap-2 text-sm text-gray-600">
          <input
            type="checkbox"
            checked={adminOnly}
            onChange={(e) => { setAdminOnly(e.target.checked); setPage(0) }}
          />
          Admins only
        </label>
        <button
          onClick={() => void refetch()}
          disabled={isLoading}
          className="text-sm text-blue-600 hover:text-blue-800 font-medium disabled:opacity-50"
        >
          Refresh
        </button>
      </div>

      {error && (
        <p className="text-sm text-red-600">Could not load users: {errorText(error)}</p>
      )}

      <div className="bg-white border border-gray-200 rounded-xl shadow-sm overflow-hidden">
        <TableScroll label="Users" minWidth={900} testId="users-table">
          <table className="w-full text-sm text-left">
            <thead className="bg-gray-50 text-gray-600 font-medium">
              <tr>
                <th className="px-4 py-3">Email</th>
                <th className="px-4 py-3">Admin</th>
                <th className="px-4 py-3">Subscription</th>
                <th className="px-4 py-3">LinkedIn</th>
                <th className="px-4 py-3">Last login</th>
                <th className="px-4 py-3">Signed up</th>
                <th className="px-4 py-3 text-right">Actions</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-100">
              {items.map((item) => (
                <Fragment key={item.id}>
                  <tr className="hover:bg-gray-50 align-top">
                    <td className="px-4 py-3">
                      <button
                        onClick={() => setOpenId(openId === item.id ? null : item.id)}
                        className="text-blue-600 hover:underline text-left"
                        aria-expanded={openId === item.id}
                      >
                        {item.email}
                      </button>
                      {item.id === user?.userId && (
                        <span className="ml-2 text-xs text-gray-400">(you)</span>
                      )}
                      {item.disabled && (
                        <span
                          data-testid={`disabled-badge-${item.id}`}
                          className="ml-2 inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium bg-red-100 text-red-800"
                        >
                          Disabled
                        </span>
                      )}
                    </td>
                    <td className="px-4 py-3">
                      {item.is_admin ? (
                        <span
                          data-testid={`admin-badge-${item.id}`}
                          className="inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium bg-blue-100 text-blue-800"
                        >
                          Admin
                          {item.admin_via_allowlist && !item.admin_via_column && ' (env)'}
                        </span>
                      ) : (
                        <span className="text-gray-400">—</span>
                      )}
                    </td>
                    <td className="px-4 py-3">
                      <span className={classNames(
                        'inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium',
                        item.subscription_status === 'active' && 'bg-green-100 text-green-800',
                        item.subscription_status === 'trial' && 'bg-amber-100 text-amber-800',
                        !['active', 'trial'].includes(item.subscription_status ?? '')
                          && 'bg-gray-100 text-gray-600'
                      )}>
                        {SUBSCRIPTION_LABELS[item.subscription_status ?? ''] ?? 'Unknown'}
                      </span>
                      {item.subscription_tier && (
                        <span className="ml-2 text-xs text-gray-500">{item.subscription_tier}</span>
                      )}
                    </td>
                    <td className="px-4 py-3">
                      <span className={classNames(
                        item.linkedin_connection_status === 'connected'
                          ? 'text-gray-800' : 'text-amber-700'
                      )}>
                        {CONNECTION_LABELS[item.linkedin_connection_status ?? ''] ?? 'Unknown'}
                      </span>
                    </td>
                    <td className="px-4 py-3 text-gray-500">{when(item.last_login)}</td>
                    <td className="px-4 py-3 text-gray-500">{when(item.signed_up_at)}</td>
                    <td className="px-4 py-3 text-right">
                      <div className="flex flex-col items-end gap-1">
                        <button
                          onClick={() => handleRole(item)}
                          disabled={role.isPending}
                          className={classNames(
                            'text-xs px-2.5 py-1 rounded disabled:opacity-50',
                            item.is_admin
                              ? 'bg-gray-100 text-gray-700 hover:bg-gray-200'
                              : 'bg-blue-600 text-white hover:bg-blue-700'
                          )}
                        >
                          {role.isPending && actedId === item.id
                            ? 'Working…'
                            : item.is_admin ? 'Remove admin' : 'Make admin'}
                        </button>
                        {actedId === item.id && lastAction === 'role' && !role.isPending && roleError && (
                          <p className="text-xs max-w-xs text-right text-amber-700">{roleError}</p>
                        )}
                        {actedId === item.id && lastAction === 'role' && !role.isPending && !roleError
                          && role.data?.changed === false && (
                          <p className="text-xs text-gray-500">Already in that state</p>
                        )}
                        <button
                          onClick={() => handleDisable(item)}
                          disabled={disabling.isPending || item.id === user?.userId}
                          title={item.id === user?.userId ? 'You cannot disable your own account' : undefined}
                          className={classNames(
                            'text-xs px-2.5 py-1 rounded disabled:opacity-50',
                            item.disabled
                              ? 'bg-green-50 text-green-700 hover:bg-green-100'
                              : 'bg-red-50 text-red-700 hover:bg-red-100'
                          )}
                        >
                          {disabling.isPending && actedId === item.id
                            ? 'Working…'
                            : item.disabled ? 'Enable' : 'Disable'}
                        </button>
                        {actedId === item.id && lastAction === 'disabled' && !disabling.isPending && disabledError && (
                          <p className="text-xs max-w-xs text-right text-amber-700">{disabledError}</p>
                        )}
                        {actedId === item.id && lastAction === 'disabled' && !disabling.isPending
                          && !disabledError && disabling.data?.changed === false && (
                          <p className="text-xs text-gray-500">Already in that state</p>
                        )}
                      </div>
                    </td>
                  </tr>
                  {/* Its OWN full-width row, not a div inside the email cell: a 22-field detail
                      nested in the first column stretches that column to half the table and shifts
                      every other one sideways for as long as the drawer is open. */}
                  {openId === item.id && (
                    <tr className="bg-gray-50">
                      <td colSpan={7} className="px-4 py-3">
                        <UserDetail userId={item.id} sessionToken={token} />
                      </td>
                    </tr>
                  )}
                </Fragment>
              ))}
              {items.length === 0 && !isLoading && (
                <tr>
                  <td colSpan={7} className="px-4 py-8 text-center text-gray-500">
                    No users match the current filters.
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
            : 'No users'}
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
