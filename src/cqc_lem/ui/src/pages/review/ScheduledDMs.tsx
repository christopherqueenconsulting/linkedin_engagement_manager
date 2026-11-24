import { useEffect, useRef, useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import api from '../../api/client'
import type { components } from '../../api/schema'
import { useAuth } from '../../contexts/useAuth'
import { formatInTimezone, zonedInputToUtcIso } from '../../utils/datetime'
import { maskProps } from '../../utils/analytics'

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
  authenticity_score?: number | null
  authenticity_issues?: string | null
}

// Auto-drafted rows (issue #485: the next message in a thread where the lead replied) land in this
// same queue as 'pending'. The badge is what tells the operator they are approving a machine's
// draft rather than something they wrote.
// An unmapped source renders NO badge at all, so a lane that queues drafts has to name itself here
// or its drafts arrive with no provenance (issue #1137).
const SOURCE_LABELS: Record<string, string> = {
  nurture: 'AUTO-DRAFTED REPLY',
  profile_viewer: 'VIEWED YOUR PROFILE',
}

const STATUS_COLORS: Record<string, string> = {
  pending: 'bg-warning/10 text-warning',
  approved: 'bg-success/10 text-success',
  scheduled: 'bg-accent/10 text-accent',
  sent: 'bg-accent/10 text-accent',
  failed: 'bg-danger/10 text-danger',
  canceled: 'bg-page text-fg-muted',
}

const STATUSES = ['ALL', 'pending', 'approved', 'scheduled', 'sent', 'failed', 'canceled']

function errorText(err: unknown, fallback: string): string {
  const detail = (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail
  return typeof detail === 'string' ? detail : fallback
}

export default function ScheduledDMs(
  { userTimezone, timezoneResolved }: { userTimezone: string; timezoneResolved: boolean },
) {
  const { sessionToken } = useAuth()
  const qc = useQueryClient()
  const [filter, setFilter] = useState('ALL')
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null)
  const [override, setOverride] = useState<{ id: number; detail: { message: string; score: number; issues: string[] } } | null>(null)
  const [reason, setReason] = useState('')
  const reasonRef = useRef<HTMLTextAreaElement>(null)

  useEffect(() => {
    if (!override) return
    reasonRef.current?.focus()
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') { setOverride(null); setReason('') }
    }
    document.addEventListener('keydown', onKeyDown)
    return () => document.removeEventListener('keydown', onKeyDown)
  }, [override])

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
    // …and not against a GUESS of it either (issue #774) — until /user/timezone answers,
    // `userTimezone` is the browser's zone and the send would land offset by the difference.
    if (!timezoneResolved) {
      flash(false, 'Your timezone has not loaded yet — reload the page so this sends at the time you pick.')
      return
    }
    const scheduledUtc = zonedInputToUtcIso(when, userTimezone)
    if (!scheduledUtc) { flash(false, 'Scheduled date/time is not valid.'); return }
    createMutation.mutate({ status, scheduledUtc })
  }

  // Approve and Cancel used to report NOTHING (issue #1528). A refused write — a stale row, a
  // scope refusal, a dropped connection — left the badge exactly where it was and said nothing, so
  // the only reading available to the operator was that the button is dead. Both outcomes are
  // flashed now: the failure names itself, and the success says what will happen next.
  const actionMutation = useMutation({
    mutationFn: (v: { id: number; action: 'approve' | 'cancel'; reason?: string }) => {
      const request: components['schemas']['UpdateDmRequest'] = {
        session_token: sessionToken!, dm_id: v.id, action: v.action,
        ...(v.reason ? { authenticity_override_reason: v.reason } : {}),
      }
      return api.put('/dm', request)
    },
    onSuccess: (_r, v) => {
      invalidate()
      flash(true, v.action === 'approve'
        ? 'Approved — it will send at its scheduled time.'
        : 'Canceled — this DM will not be sent.')
    },
    onError: (err, v) => {
      const detail = (err as { response?: { status?: number; data?: { detail?: unknown } } })?.response?.data?.detail
      if (v.action === 'approve' && (err as { response?: { status?: number } })?.response?.status === 403 &&
          typeof detail === 'object' && detail !== null && (detail as { code?: string }).code === 'authenticity_below_floor') {
        const floor = detail as { message?: string; score?: number; issues?: string[] }
        setOverride({ id: v.id, detail: { message: floor.message ?? 'This draft is below the authenticity floor.',
          score: floor.score ?? 0, issues: floor.issues ?? [] } })
        setReason('')
        return
      }
      flash(false, errorText(err, v.action === 'approve'
        ? 'Could not approve this DM — try again.'
        : 'Could not cancel this DM — try again.'))
    },
  })

  const submitOverride = () => {
    const trimmed = reason.trim()
    if (!trimmed || trimmed.length > 500) return
    const id = override?.id
    setOverride(null)
    setReason('')
    if (id !== undefined) actionMutation.mutate({ id, action: 'approve', reason: trimmed })
  }

  const canSubmit = url.trim() && body.trim() && when && timezoneResolved

  return (
    <div className="space-y-4">
      {msg && <p className={`text-sm font-medium ${msg.ok ? 'text-success' : 'text-danger'}`}>{msg.text}</p>}

      {/* New scheduled DM */}
      <div className="bg-raised rounded-panel border border-hairline shadow-panel p-5 space-y-3">
        <h3 className="font-semibold text-fg">Schedule a DM</h3>
        <p className="text-xs text-fg-muted">
          Draft a 1:1 message to a connection and send it at a chosen time. Must be a 1st-degree
          connection. Sends honor your daily DM cap.
        </p>
        <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
          <input type="url" value={url} onChange={(e) => setUrl(e.target.value)} maxLength={512}
            placeholder="Recipient profile URL (https://www.linkedin.com/in/…)"
            className="w-full border border-field rounded-control px-3 py-2 text-sm" />
          <input type="text" value={name} onChange={(e) => setName(e.target.value)} maxLength={255}
            placeholder="Recipient name (optional)"
            className="w-full border border-field rounded-control px-3 py-2 text-sm" />
        </div>
        <textarea value={body} onChange={(e) => setBody(e.target.value)} rows={3} maxLength={MSG_MAX}
          placeholder="Your message…"
          {...maskProps('w-full border border-field rounded-panel px-3 py-2 text-sm')} />
        <div className="flex flex-wrap items-center gap-3">
          <label className="flex flex-wrap items-center gap-2 text-xs text-fg-muted">
            <input type="datetime-local" value={when} onChange={(e) => setWhen(e.target.value)}
              className="border border-field rounded-control px-3 py-2 text-sm" />
            {timezoneResolved ? userTimezone : 'loading your timezone…'}
          </label>
          <button onClick={() => submit('pending')} disabled={!canSubmit || createMutation.isPending}
            className="px-4 py-2 border border-field rounded-control text-sm text-fg hover:bg-page disabled:opacity-50">
            Save draft
          </button>
          <button onClick={() => submit('approved')} disabled={!canSubmit || createMutation.isPending}
            className="px-4 py-2 bg-accent text-white rounded-control text-sm font-semibold hover:bg-accent/90 disabled:opacity-50">
            {createMutation.isPending ? 'Scheduling…' : 'Approve & schedule'}
          </button>
        </div>
      </div>

      {/* Status filter */}
      <div className="flex gap-2 flex-wrap">
        {STATUSES.map((s) => (
          <button key={s} onClick={() => setFilter(s)}
            className={`px-3 py-1.5 rounded-full text-xs font-semibold transition-colors ${
              filter === s ? 'bg-accent text-white' : 'bg-raised text-fg-muted border border-field hover:border-accent/50'
            }`}>
            {s.toUpperCase()}
          </button>
        ))}
      </div>

      {isLoading && <p className="text-fg-muted text-sm">Loading scheduled DMs…</p>}
      {!isLoading && dms.length === 0 && (
        <div className="text-center py-10 bg-raised rounded-panel border border-hairline text-sm text-fg-muted">
          No scheduled DMs yet.
        </div>
      )}

      <div className="space-y-3">
        {dms.map((dm) => (
          <div key={dm.id} className="bg-raised rounded-panel border border-hairline p-4">
            <div className="flex flex-wrap items-center justify-between gap-x-2 gap-y-1 mb-1">
              <a href={dm.recipient_profile_url} target="_blank" rel="noopener noreferrer"
                className="text-sm font-medium text-accent truncate hover:underline">
                {dm.recipient_name || dm.recipient_profile_url}
              </a>
              <div className="flex flex-wrap items-center gap-2">
                {dm.source && SOURCE_LABELS[dm.source] && (
                  <span className="px-2 py-0.5 rounded-full text-xs font-medium bg-accent/10 text-accent">
                    {SOURCE_LABELS[dm.source]}
                  </span>
                )}
                <span className="text-xs text-fg-muted">{formatInTimezone(dm.scheduled_time, userTimezone)}</span>
                <span className={`px-2 py-0.5 rounded-full text-xs font-medium ${STATUS_COLORS[dm.status] ?? 'bg-page text-fg-muted'}`}>
                  {dm.status.toUpperCase()}
                </span>
              </div>
            </div>
            <p className="text-sm text-fg whitespace-pre-wrap line-clamp-3">{dm.message}</p>
            {dm.status === 'pending' && dm.authenticity_score !== null && dm.authenticity_score !== undefined && (
              <div className="mt-2 text-xs text-fg-muted" aria-label="Authenticity assessment">
                <p>Authenticity score: <strong>{dm.authenticity_score}</strong></p>
                {dm.authenticity_issues && <ul className="list-disc pl-5">{dm.authenticity_issues.split('\n').filter(Boolean).map((issue, i) =>
                  <li key={`${i}-${issue}`}>{issue}</li>)}</ul>}
              </div>
            )}
            {['pending', 'approved', 'scheduled'].includes(dm.status) && (
              <div className="flex gap-2 mt-2">
                {dm.status === 'pending' && (
                  <button onClick={() => actionMutation.mutate({ id: dm.id, action: 'approve' })}
                    disabled={actionMutation.isPending}
                    className="px-3 py-1 bg-accent text-on-accent rounded-control text-xs font-semibold hover:bg-accent/90 disabled:opacity-50">
                    Approve
                  </button>
                )}
                <button onClick={() => actionMutation.mutate({ id: dm.id, action: 'cancel' })}
                  disabled={actionMutation.isPending}
                  className="px-3 py-1 border border-field text-fg-muted rounded-control text-xs font-semibold hover:bg-page disabled:opacity-50">
                  Cancel
                </button>
              </div>
            )}
          </div>
        ))}
      </div>
      {override && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4" onMouseDown={(e) => { if (e.target === e.currentTarget) { setOverride(null); setReason('') } }}>
          <section role="dialog" aria-modal="true" aria-labelledby="authenticity-override-title" aria-describedby="authenticity-override-description"
            className="w-full max-w-lg rounded-panel bg-raised p-5 shadow-panel space-y-3">
            <h2 id="authenticity-override-title" className="font-semibold text-fg">Confirm authenticity override</h2>
            <p id="authenticity-override-description" className="text-sm text-fg-muted">{override.detail.message} Score: {override.detail.score}.</p>
            {override.detail.issues.length > 0 && <ul className="list-disc pl-5 text-sm text-fg">{override.detail.issues.map((issue, i) => <li key={`${i}-${issue}`}>{issue}</li>)}</ul>}
            <label htmlFor="authenticity-override-reason" className="block text-sm font-medium text-fg">Reason for approving anyway</label>
            <textarea id="authenticity-override-reason" ref={reasonRef} value={reason} maxLength={500} rows={3}
              onChange={(e) => setReason(e.target.value)} className="w-full border border-field rounded-control px-3 py-2 text-sm" />
            <p className="text-xs text-fg-muted">{reason.length}/500 characters. Required.</p>
            <div className="flex justify-end gap-2">
              <button onClick={() => { setOverride(null); setReason('') }} className="px-3 py-2 border border-field rounded-control text-sm">Cancel</button>
              <button onClick={submitOverride} disabled={!reason.trim() || reason.trim().length > 500 || actionMutation.isPending}
                className="px-3 py-2 bg-accent text-on-accent rounded-control text-sm font-semibold disabled:opacity-50">Approve anyway</button>
            </div>
          </section>
        </div>
      )}
    </div>
  )
}
