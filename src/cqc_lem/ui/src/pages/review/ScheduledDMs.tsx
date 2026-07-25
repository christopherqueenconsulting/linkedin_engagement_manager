import { useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import api from '../../api/client'
import { useAuth } from '../../contexts/AuthContext'
import { formatInTimezone, zonedInputToUtcIso } from '../../utils/datetime'

// Schedule 1:1 DMs to chosen recipients at chosen times (issue #306), mirroring the scheduled-posts
// preview/approve workflow. Drafts are 'pending'; approving queues them for the scanner to send at
// their scheduled time (honoring the per-day DM cap).
const MSG_MAX = 2000

interface ScheduledDm {
  id: number
  recipient_profile_url: string
  recipient_name: string | null
  message: string
  scheduled_time: string
  status: string
  source?: string | null
}

// Auto-drafted rows (issue #485: the next message in a thread where the lead replied) land in this
// same queue as 'pending'. The badge is what tells the operator they are approving a machine's
// draft rather than something they wrote.
const SOURCE_LABELS: Record<string, string> = { nurture: 'AUTO-DRAFTED REPLY' }

const STATUS_COLORS: Record<string, string> = {
  pending: 'bg-yellow-100 text-yellow-700',
  approved: 'bg-green-100 text-green-700',
  scheduled: 'bg-purple-100 text-purple-700',
  sent: 'bg-blue-100 text-blue-700',
  failed: 'bg-red-100 text-red-700',
  canceled: 'bg-gray-100 text-gray-500',
}

const STATUSES = ['ALL', 'pending', 'approved', 'scheduled', 'sent', 'failed', 'canceled']

export default function ScheduledDMs({ userTimezone }: { userTimezone: string }) {
  const { sessionToken } = useAuth()
  const qc = useQueryClient()
  const [filter, setFilter] = useState('ALL')
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null)

  // New-DM draft form
  const [url, setUrl] = useState('')
  const [name, setName] = useState('')
  const [body, setBody] = useState('')
  const [when, setWhen] = useState('')

  const queryKey = ['scheduled-dms', sessionToken, filter]
  const { data, isLoading } = useQuery({
    queryKey,
    queryFn: () => {
      const p = new URLSearchParams({ session_token: sessionToken!, page: '1', page_size: '50' })
      if (filter !== 'ALL') p.set('status_filter', filter)
      return api.get(`/dms?${p.toString()}`).then((r) => r.data.detail as { dms: ScheduledDm[]; total: number })
    },
    enabled: !!sessionToken,
  })
  const dms = data?.dms ?? []

  const flash = (ok: boolean, text: string) => { setMsg({ ok, text }); setTimeout(() => setMsg(null), 4000) }
  const invalidate = () => qc.invalidateQueries({ queryKey: ['scheduled-dms'] })

  const createMutation = useMutation({
    mutationFn: (v: { status: 'pending' | 'approved'; scheduledUtc: string }) =>
      api.post('/schedule_dm', {
        session_token: sessionToken,
        recipient_profile_url: url.trim(),
        recipient_name: name.trim() || null,
        message: body,
        scheduled_datetime: v.scheduledUtc,
        status: v.status,
      }),
    onSuccess: () => {
      invalidate(); setUrl(''); setName(''); setBody(''); setWhen('')
      flash(true, 'DM scheduled.')
    },
    onError: () => flash(false, 'Could not schedule — check the fields and try again.'),
  })

  // The picker is a bare wall clock — read it in the user's timezone, not the browser's. An
  // unparseable value yields null, which the API would reject with a 422; catch it here instead.
  const submit = (status: 'pending' | 'approved') => {
    const scheduledUtc = zonedInputToUtcIso(when, userTimezone)
    if (!scheduledUtc) { flash(false, 'Scheduled date/time is not valid.'); return }
    createMutation.mutate({ status, scheduledUtc })
  }

  const actionMutation = useMutation({
    mutationFn: (v: { id: number; action: 'approve' | 'cancel' }) =>
      api.put('/dm', { session_token: sessionToken, dm_id: v.id, action: v.action }),
    onSuccess: () => invalidate(),
  })

  const canSubmit = url.trim() && body.trim() && when

  return (
    <div className="space-y-4">
      {msg && <p className={`text-sm font-medium ${msg.ok ? 'text-green-600' : 'text-red-600'}`}>{msg.text}</p>}

      {/* New scheduled DM */}
      <div className="bg-white rounded-lg border border-gray-200 shadow-sm p-5 space-y-3">
        <h3 className="font-semibold text-gray-700">Schedule a DM</h3>
        <p className="text-xs text-gray-500">
          Draft a 1:1 message to a connection and send it at a chosen time. Must be a 1st-degree
          connection. Sends honor your daily DM cap.
        </p>
        <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
          <input type="url" value={url} onChange={(e) => setUrl(e.target.value)} maxLength={512}
            placeholder="Recipient profile URL (https://www.linkedin.com/in/…)"
            className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm" />
          <input type="text" value={name} onChange={(e) => setName(e.target.value)} maxLength={255}
            placeholder="Recipient name (optional)"
            className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm" />
        </div>
        <textarea value={body} onChange={(e) => setBody(e.target.value)} rows={3} maxLength={MSG_MAX}
          placeholder="Your message…"
          className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm" />
        <div className="flex flex-wrap items-center gap-3">
          <label className="flex items-center gap-2 text-xs text-gray-500">
            <input type="datetime-local" value={when} onChange={(e) => setWhen(e.target.value)}
              className="border border-gray-300 rounded-lg px-3 py-2 text-sm" />
            {userTimezone}
          </label>
          <button onClick={() => submit('pending')} disabled={!canSubmit || createMutation.isPending}
            className="px-4 py-2 border border-gray-300 rounded-lg text-sm text-gray-700 hover:bg-gray-50 disabled:opacity-50">
            Save draft
          </button>
          <button onClick={() => submit('approved')} disabled={!canSubmit || createMutation.isPending}
            className="px-4 py-2 bg-blue-600 text-white rounded-lg text-sm font-semibold hover:bg-blue-700 disabled:opacity-50">
            {createMutation.isPending ? 'Scheduling…' : 'Approve & schedule'}
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

      {isLoading && <p className="text-gray-400 text-sm">Loading scheduled DMs…</p>}
      {!isLoading && dms.length === 0 && (
        <div className="text-center py-10 bg-white rounded-lg border border-gray-200 text-sm text-gray-500">
          No scheduled DMs yet.
        </div>
      )}

      <div className="space-y-3">
        {dms.map((dm) => (
          <div key={dm.id} className="bg-white rounded-lg border border-gray-200 p-4">
            <div className="flex items-center justify-between gap-2 mb-1">
              <a href={dm.recipient_profile_url} target="_blank" rel="noopener noreferrer"
                className="text-sm font-medium text-blue-700 truncate hover:underline">
                {dm.recipient_name || dm.recipient_profile_url}
              </a>
              <div className="flex items-center gap-2 shrink-0">
                {dm.source && SOURCE_LABELS[dm.source] && (
                  <span className="px-2 py-0.5 rounded-full text-xs font-medium bg-indigo-100 text-indigo-700">
                    {SOURCE_LABELS[dm.source]}
                  </span>
                )}
                <span className="text-xs text-gray-400">{formatInTimezone(dm.scheduled_time, userTimezone)}</span>
                <span className={`px-2 py-0.5 rounded-full text-xs font-medium ${STATUS_COLORS[dm.status] ?? 'bg-gray-100 text-gray-600'}`}>
                  {dm.status.toUpperCase()}
                </span>
              </div>
            </div>
            <p className="text-sm text-gray-700 whitespace-pre-wrap line-clamp-3">{dm.message}</p>
            {['pending', 'approved', 'scheduled'].includes(dm.status) && (
              <div className="flex gap-2 mt-2">
                {dm.status === 'pending' && (
                  <button onClick={() => actionMutation.mutate({ id: dm.id, action: 'approve' })}
                    disabled={actionMutation.isPending}
                    className="px-3 py-1 bg-green-600 text-white rounded text-xs font-semibold hover:bg-green-700 disabled:opacity-50">
                    Approve
                  </button>
                )}
                <button onClick={() => actionMutation.mutate({ id: dm.id, action: 'cancel' })}
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
