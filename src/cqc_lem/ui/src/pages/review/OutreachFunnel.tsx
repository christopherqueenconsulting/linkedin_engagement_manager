import { useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import api from '../../api/client'
import { useAuth } from '../../contexts/AuthContext'
import { maskProps } from '../../utils/analytics'

// Comment-first outreach funnel (issue #399): a sequenced, APPROVAL-GATED value-comment -> connect
// -> DM funnel keyed to a target list. Every stage is drafted as 'pending' and only fires once a
// human approves it; approving one stage advances the target to the next stage as 'pending' again —
// so no step ever auto-fires at volume.
const DRAFT_MAX = 3000

interface OutreachTarget {
  id: number
  target_profile_url: string
  target_name: string | null
  stage: string
  status: string
  context_url: string | null
  draft_text: string | null
  updated_at: string | null
}

const STAGE_LABELS: Record<string, string> = {
  comment: '1 · Comment',
  connect: '2 · Connect',
  dm: '3 · DM',
  completed: '✓ Completed',
}

const STAGE_HELP: Record<string, string> = {
  comment: 'Leave a value-adding comment on their post (needs a post URL).',
  connect: 'Send a connection request with this note.',
  dm: 'Send this voice-aligned DM (must be a 1st-degree connection).',
  completed: 'The DM has been sent — this target has finished the funnel.',
}

const STATUS_COLORS: Record<string, string> = {
  pending: 'bg-yellow-100 text-yellow-700',
  approved: 'bg-green-100 text-green-700',
  acted: 'bg-blue-100 text-blue-700',
  skipped: 'bg-gray-100 text-gray-500',
  failed: 'bg-red-100 text-red-700',
  canceled: 'bg-gray-100 text-gray-500',
}

const STATUSES = ['ALL', 'pending', 'approved', 'acted', 'skipped', 'failed', 'canceled']

export default function OutreachFunnel() {
  const { sessionToken } = useAuth()
  const qc = useQueryClient()
  const [filter, setFilter] = useState('ALL')
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null)

  // New-target draft form
  const [url, setUrl] = useState('')
  const [name, setName] = useState('')
  const [contextUrl, setContextUrl] = useState('')
  const [draft, setDraft] = useState('')

  // Per-target draft edits (keyed by id) so each card edits independently.
  const [edits, setEdits] = useState<Record<number, string>>({})

  const queryKey = ['outreach-targets', sessionToken, filter]
  const { data, isLoading } = useQuery({
    queryKey,
    queryFn: () => {
      const p = new URLSearchParams({ session_token: sessionToken!, page: '1', page_size: '50' })
      if (filter !== 'ALL') p.set('status_filter', filter)
      return api.get(`/outreach/targets?${p.toString()}`).then((r) => r.data.detail as { targets: OutreachTarget[]; total: number })
    },
    enabled: !!sessionToken,
  })
  const targets = data?.targets ?? []

  const flash = (ok: boolean, text: string) => { setMsg({ ok, text }); setTimeout(() => setMsg(null), 4000) }
  const invalidate = () => qc.invalidateQueries({ queryKey: ['outreach-targets'] })

  const createMutation = useMutation({
    mutationFn: (status: 'pending' | 'approved') =>
      api.post('/outreach/target', {
        session_token: sessionToken,
        target_profile_url: url.trim(),
        target_name: name.trim() || null,
        context_url: contextUrl.trim() || null,
        draft_text: draft.trim() || null,
        status,
      }),
    onSuccess: () => {
      invalidate(); setUrl(''); setName(''); setContextUrl(''); setDraft('')
      flash(true, 'Target added to the funnel.')
    },
    onError: (e: unknown) => {
      const status = (e as { response?: { status?: number } })?.response?.status
      flash(false, status === 409 ? 'That profile is already in your funnel.' : 'Could not add — check the fields and try again.')
    },
  })

  const saveMutation = useMutation({
    mutationFn: (v: { id: number; draft_text: string }) =>
      api.put('/outreach/target', { session_token: sessionToken, target_id: v.id, draft_text: v.draft_text }),
    onSuccess: (_r, v) => { invalidate(); setEdits((e) => { const n = { ...e }; delete n[v.id]; return n }); flash(true, 'Draft saved.') },
    onError: () => flash(false, 'Could not save the draft.'),
  })

  const actionMutation = useMutation({
    mutationFn: (v: { id: number; action: 'approve' | 'cancel' }) =>
      api.put('/outreach/target', { session_token: sessionToken, target_id: v.id, action: v.action }),
    onSuccess: () => invalidate(),
  })

  const canSubmit = url.trim().length > 0

  return (
    <div className="space-y-4">
      {msg && <p className={`text-sm font-medium ${msg.ok ? 'text-green-600' : 'text-red-600'}`}>{msg.text}</p>}

      {/* New target */}
      <div className="bg-white rounded-lg border border-gray-200 shadow-sm p-5 space-y-3">
        <h3 className="font-semibold text-gray-700">Add a target to the funnel</h3>
        <p className="text-xs text-gray-500">
          Each target moves comment → connect → DM, and <span className="font-semibold">every step needs your
          approval</span> before it fires. Approving a step advances the target to the next step for review — nothing
          sends automatically.
        </p>
        <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
          <input type="url" value={url} onChange={(e) => setUrl(e.target.value)} maxLength={512}
            placeholder="Target profile URL (https://www.linkedin.com/in/…)"
            className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm" />
          <input type="text" value={name} onChange={(e) => setName(e.target.value)} maxLength={255}
            placeholder="Target name (optional)"
            className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm" />
        </div>
        <input type="url" value={contextUrl} onChange={(e) => setContextUrl(e.target.value)} maxLength={512}
          placeholder="Post URL to comment on (the first step)"
          className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm" />
        <textarea value={draft} onChange={(e) => setDraft(e.target.value)} rows={3} maxLength={DRAFT_MAX}
          placeholder="Your value-adding comment…"
          {...maskProps('w-full border border-gray-300 rounded-lg px-3 py-2 text-sm')} />
        <div className="flex flex-wrap items-center gap-3">
          <button onClick={() => createMutation.mutate('pending')} disabled={!canSubmit || createMutation.isPending}
            className="px-4 py-2 border border-gray-300 rounded-lg text-sm text-gray-700 hover:bg-gray-50 disabled:opacity-50">
            Save draft
          </button>
          <button onClick={() => createMutation.mutate('approved')} disabled={!canSubmit || createMutation.isPending}
            className="px-4 py-2 bg-blue-600 text-white rounded-lg text-sm font-semibold hover:bg-blue-700 disabled:opacity-50">
            {createMutation.isPending ? 'Adding…' : 'Add & approve comment'}
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

      {isLoading && <p className="text-gray-400 text-sm">Loading funnel…</p>}
      {!isLoading && targets.length === 0 && (
        <div className="text-center py-10 bg-white rounded-lg border border-gray-200 text-sm text-gray-500">
          No targets in the funnel yet.
        </div>
      )}

      <div className="space-y-3">
        {targets.map((t) => {
          const editing = edits[t.id] !== undefined
          const draftValue = editing ? edits[t.id] : (t.draft_text ?? '')
          const terminal = ['completed'].includes(t.stage) || ['canceled', 'acted'].includes(t.status)
          return (
            <div key={t.id} className="bg-white rounded-lg border border-gray-200 p-4 space-y-2">
              <div className="flex items-center justify-between gap-2">
                <a href={t.target_profile_url} target="_blank" rel="noopener noreferrer"
                  className="text-sm font-medium text-blue-700 truncate hover:underline">
                  {t.target_name || t.target_profile_url}
                </a>
                <div className="flex items-center gap-2 shrink-0">
                  <span className="px-2 py-0.5 rounded-full text-xs font-medium bg-indigo-100 text-indigo-700">
                    {STAGE_LABELS[t.stage] ?? t.stage}
                  </span>
                  <span className={`px-2 py-0.5 rounded-full text-xs font-medium ${STATUS_COLORS[t.status] ?? 'bg-gray-100 text-gray-600'}`}>
                    {t.status.toUpperCase()}
                  </span>
                </div>
              </div>
              <p className="text-xs text-gray-400">{STAGE_HELP[t.stage] ?? ''}</p>

              {!terminal && (
                <>
                  <textarea
                    value={draftValue}
                    onChange={(e) => setEdits((prev) => ({ ...prev, [t.id]: e.target.value }))}
                    rows={3} maxLength={DRAFT_MAX}
                    placeholder={t.stage === 'comment' ? 'Comment text…' : t.stage === 'connect' ? 'Connection note…' : 'DM message…'}
                    {...maskProps('w-full border border-gray-300 rounded-lg px-3 py-2 text-sm')} />
                  <div className="flex gap-2 flex-wrap">
                    <button onClick={() => saveMutation.mutate({ id: t.id, draft_text: draftValue })}
                      disabled={!editing || saveMutation.isPending}
                      className="px-3 py-1 border border-gray-300 text-gray-600 rounded text-xs font-semibold hover:bg-gray-50 disabled:opacity-50">
                      Save draft
                    </button>
                    {['pending', 'failed', 'skipped'].includes(t.status) && (
                      <button onClick={() => actionMutation.mutate({ id: t.id, action: 'approve' })}
                        disabled={actionMutation.isPending}
                        className="px-3 py-1 bg-green-600 text-white rounded text-xs font-semibold hover:bg-green-700 disabled:opacity-50">
                        {t.status === 'pending' ? 'Approve' : 'Retry'} {t.stage}
                      </button>
                    )}
                    <button onClick={() => actionMutation.mutate({ id: t.id, action: 'cancel' })}
                      disabled={actionMutation.isPending}
                      className="px-3 py-1 border border-gray-300 text-gray-600 rounded text-xs font-semibold hover:bg-gray-50 disabled:opacity-50">
                      Cancel
                    </button>
                  </div>
                </>
              )}
              {terminal && t.draft_text && (
                <p className="text-sm text-gray-700 whitespace-pre-wrap line-clamp-3">{t.draft_text}</p>
              )}
            </div>
          )
        })}
      </div>
    </div>
  )
}
