import { useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import api from '../../api/client'
import { useAuth } from '../../contexts/useAuth'
import { formatInTimezone } from '../../utils/datetime'
import { maskProps } from '../../utils/analytics'

// Proactive, human-approved connection requests (issue #398). Add ICP-targeted prospects, approve
// them, and LEM sends the invite (reusing invite_to_connect) on a slow, daily-capped drip that honors
// the rate-limit / kill-switch. Drafts are 'pending'; approving queues them. NO volume prospecting —
// each request is approved by a human and the per-day cap (Account → Max invites/day) is enforced.
const NOTE_MAX = 300

interface ConnectionRequest {
  id: number
  recipient_profile_url: string
  recipient_name: string | null
  message: string | null
  status: string
  created_at: string
  // Targeting provenance (issue #486) — null for hand-added targets. icp_score is null when we have
  // no profile facts to score fit against (issue #623), which is most engagers.
  source: string | null
  icp_score: number | null
  reasons: string | null
  // Why a send failed (issue #623) — e.g. already connected, or no Connect button on the profile.
  failure_reason: string | null
}

const SOURCE_LABELS: Record<string, string> = {
  own_post: 'Engaged with your content',
  adjacent_post: 'Engaged with an adjacent author',
  // Issue #1137 — without this the badge falls through to the raw column value.
  profile_viewer: 'Viewed your profile',
}

const STATUS_COLORS: Record<string, string> = {
  pending: 'bg-yellow-100 text-yellow-700',
  approved: 'bg-green-100 text-green-700',
  sending: 'bg-purple-100 text-purple-700',
  sent: 'bg-blue-100 text-blue-700',
  failed: 'bg-red-100 text-red-700',
  canceled: 'bg-gray-100 text-gray-500',
}

const STATUSES = ['ALL', 'pending', 'approved', 'sending', 'sent', 'failed', 'canceled']

export default function ConnectionRequests({ userTimezone }: { userTimezone: string }) {
  const { sessionToken } = useAuth()
  const qc = useQueryClient()
  const [filter, setFilter] = useState('ALL')
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null)

  // New-target form
  const [url, setUrl] = useState('')
  const [name, setName] = useState('')
  const [note, setNote] = useState('')

  const queryKey = ['connection-requests', sessionToken, filter]
  const { data, isLoading } = useQuery({
    queryKey,
    queryFn: () => {
      const p = new URLSearchParams({ session_token: sessionToken!, page: '1', page_size: '50' })
      if (filter !== 'ALL') p.set('status_filter', filter)
      return api
        .get(`/connection_requests?${p.toString()}`)
        .then((r) => r.data.detail as { requests: ConnectionRequest[]; total: number })
    },
    enabled: !!sessionToken,
  })
  const requests = data?.requests ?? []

  const flash = (ok: boolean, text: string) => { setMsg({ ok, text }); setTimeout(() => setMsg(null), 4000) }
  const invalidate = () => qc.invalidateQueries({ queryKey: ['connection-requests'] })

  // No status is sent — the server applies the account's Connection approval mode (auto-approve queues
  // it immediately; pre-review holds it as a draft here for approval).
  const createMutation = useMutation({
    mutationFn: () =>
      api.post('/connection_request', {
        session_token: sessionToken,
        recipient_profile_url: url.trim(),
        recipient_name: name.trim() || null,
        message: note.trim() || null,
      }),
    onSuccess: () => {
      invalidate(); setUrl(''); setName(''); setNote('')
      flash(true, 'Target added.')
    },
    onError: () => flash(false, 'Could not add — check the fields and try again.'),
  })

  const actionMutation = useMutation({
    mutationFn: (v: { id: number; action: 'approve' | 'cancel' }) =>
      api.put('/connection_request', { session_token: sessionToken, request_id: v.id, action: v.action }),
    onSuccess: () => invalidate(),
  })

  const canSubmit = !!url.trim()

  return (
    <div className="space-y-4">
      {msg && <p className={`text-sm font-medium ${msg.ok ? 'text-green-600' : 'text-red-600'}`}>{msg.text}</p>}

      {/* New connection-request target */}
      <div className="bg-white rounded-lg border border-gray-200 shadow-sm p-5 space-y-3">
        <h3 className="font-semibold text-gray-700">Add a connection target</h3>
        <p className="text-xs text-gray-500">
          Add an ICP-fit prospect to invite. New targets follow your account's Connection approval mode
          (Account → Connection approval): <strong>auto-approve</strong> queues them for a slow,
          daily-capped drip immediately, while <strong>pre-review</strong> holds them here as drafts for
          you to approve first. Sends honor your combined Max invites/day cap and pause automatically
          when LinkedIn throttles. This is not volume prospecting.
        </p>
        <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
          <input type="url" value={url} onChange={(e) => setUrl(e.target.value)} maxLength={512}
            placeholder="Profile URL (https://www.linkedin.com/in/…)"
            className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm" />
          <input type="text" value={name} onChange={(e) => setName(e.target.value)} maxLength={255}
            placeholder="Name (optional)"
            className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm" />
        </div>
        <textarea value={note} onChange={(e) => setNote(e.target.value)} rows={2} maxLength={NOTE_MAX}
          placeholder="Connection note (optional, ≤300 chars)"
          {...maskProps('w-full border border-gray-300 rounded-lg px-3 py-2 text-sm')} />
        <div className="flex flex-wrap items-center gap-3">
          <button onClick={() => createMutation.mutate()} disabled={!canSubmit || createMutation.isPending}
            className="px-4 py-2 bg-blue-600 text-white rounded-lg text-sm font-semibold hover:bg-blue-700 disabled:opacity-50">
            {createMutation.isPending ? 'Adding…' : 'Add target'}
          </button>
        </div>
      </div>

      {/* Status filter */}
      <div className="flex gap-2 flex-wrap">
        {STATUSES.map((s) => (
          <button key={s} onClick={() => setFilter(s)}
            className={`px-3 py-1.5 rounded-full text-xs font-semibold transition-colors ${
              filter === s ? 'bg-blue-600 text-white' : 'bg-white text-gray-600 border border-gray-300 hover:border-blue-400'
            }`}>
            {s.toUpperCase()}
          </button>
        ))}
      </div>

      {isLoading && <p className="text-gray-400 text-sm">Loading connection requests…</p>}
      {!isLoading && requests.length === 0 && (
        <div className="text-center py-10 bg-white rounded-lg border border-gray-200 text-sm text-gray-500">
          No connection requests yet.
        </div>
      )}

      <div className="space-y-3">
        {requests.map((req) => (
          <div key={req.id} className="bg-white rounded-lg border border-gray-200 p-4">
            <div className="flex items-center justify-between gap-2 mb-1">
              <a href={req.recipient_profile_url} target="_blank" rel="noopener noreferrer"
                className="text-sm font-medium text-blue-700 truncate hover:underline">
                {req.recipient_name || req.recipient_profile_url}
              </a>
              <div className="flex items-center gap-2 shrink-0">
                <span className="text-xs text-gray-400">{formatInTimezone(req.created_at, userTimezone)}</span>
                <span className={`px-2 py-0.5 rounded-full text-xs font-medium ${STATUS_COLORS[req.status] ?? 'bg-gray-100 text-gray-600'}`}>
                  {req.status.toUpperCase()}
                </span>
              </div>
            </div>
            {(req.source || req.reasons) && (
              <div className="flex flex-wrap items-center gap-2 mb-1">
                {req.source && (
                  <span className="px-2 py-0.5 rounded-full bg-indigo-50 text-indigo-700 text-xs font-medium">
                    {SOURCE_LABELS[req.source] ?? req.source}
                  </span>
                )}
                {req.icp_score != null ? (
                  <span className="text-xs text-gray-500">ICP fit {req.icp_score}/100</span>
                ) : (
                  req.source && <span className="text-xs text-gray-400">ICP fit unverified</span>
                )}
                {req.reasons && <span className="text-xs text-gray-500 truncate">· {req.reasons}</span>}
              </div>
            )}
            {req.status === 'failed' && req.failure_reason && (
              <p className="text-xs text-red-600 mb-1">Why it failed: {req.failure_reason}</p>
            )}
            {req.message && <p className="text-sm text-gray-700 whitespace-pre-wrap line-clamp-3">{req.message}</p>}
            {['pending', 'approved', 'sending'].includes(req.status) && (
              <div className="flex gap-2 mt-2">
                {req.status === 'pending' && (
                  <button onClick={() => actionMutation.mutate({ id: req.id, action: 'approve' })}
                    disabled={actionMutation.isPending}
                    className="px-3 py-1 bg-green-600 text-white rounded text-xs font-semibold hover:bg-green-700 disabled:opacity-50">
                    Approve
                  </button>
                )}
                <button onClick={() => actionMutation.mutate({ id: req.id, action: 'cancel' })}
                  disabled={actionMutation.isPending}
                  className="px-3 py-1 border border-gray-300 text-gray-600 rounded text-xs font-semibold hover:bg-gray-50 disabled:opacity-50">
                  Cancel
                </button>
              </div>
            )}
          </div>
        ))}
      </div>
    </div>
  )
}
