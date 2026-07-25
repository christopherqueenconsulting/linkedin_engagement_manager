import { useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import api from '../../api/client'
import { useAuth } from '../../contexts/AuthContext'
import { formatInTimezone } from '../../utils/datetime'

// Lead pipeline (issue #484): everyone who engages with you, scored by ICP fit weighted by how
// recently and how often they engage. Scores rebuild nightly from data LEM already has — nothing
// here scrapes anything. Move a stage by hand or add a note and the re-score leaves it alone.
const NOTES_MAX = 512

interface Lead {
  id: number
  person_name: string | null
  person_profile_url: string | null
  score: number
  icp_score: number
  engagement_score: number
  stage: string
  manual_stage: string | null
  signals: string | null
  signal_count: number
  reasons: string | null
  next_action: string | null
  notes: string | null
  dismissed: boolean
  last_signal_at: string | null
}

const STAGES = ['opportunity', 'in_conversation', 'hot', 'warm', 'cold'] as const

const STAGE_LABELS: Record<string, string> = {
  opportunity: 'Opportunity',
  in_conversation: 'In conversation',
  hot: 'Hot',
  warm: 'Warm',
  cold: 'Cold',
}

const STAGE_COLORS: Record<string, string> = {
  opportunity: 'bg-purple-100 text-purple-700',
  in_conversation: 'bg-blue-100 text-blue-700',
  hot: 'bg-orange-100 text-orange-700',
  warm: 'bg-amber-100 text-amber-700',
  cold: 'bg-gray-100 text-gray-500',
}

export default function LeadsPipeline({ userTimezone }: { userTimezone: string }) {
  const { sessionToken } = useAuth()
  const qc = useQueryClient()
  const [showDismissed, setShowDismissed] = useState(false)
  const [notes, setNotes] = useState<Record<number, string>>({})
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null)

  const { data, isLoading } = useQuery({
    queryKey: ['leads', sessionToken, showDismissed],
    queryFn: () => {
      const p = new URLSearchParams({
        session_token: sessionToken!,
        page: '1',
        page_size: '100',
        include_dismissed: String(showDismissed),
      })
      return api
        .get(`/leads?${p.toString()}`)
        .then((r) => r.data.detail as { leads: Lead[]; total: number; hot_count: number })
    },
    enabled: !!sessionToken,
  })
  const leads = data?.leads ?? []

  const flash = (ok: boolean, text: string) => { setMsg({ ok, text }); setTimeout(() => setMsg(null), 4000) }

  const updateMutation = useMutation({
    mutationFn: (v: { id: number; stage?: string; notes?: string; action?: 'dismiss' | 'restore' }) =>
      api.put('/lead', {
        session_token: sessionToken,
        lead_id: v.id,
        stage: v.stage ?? null,
        notes: v.notes ?? null,
        action: v.action ?? null,
      }),
    onSuccess: (_r, v) => {
      qc.invalidateQueries({ queryKey: ['leads'] })
      flash(true, v.action === 'dismiss' ? 'Removed from your board.'
        : v.action === 'restore' ? 'Back on your board.'
        : v.stage !== undefined ? 'Stage updated.' : 'Note saved.')
    },
    onError: () => flash(false, 'Could not update this lead — try again.'),
  })

  const refreshMutation = useMutation({
    mutationFn: () => api.post('/leads/refresh', { session_token: sessionToken }),
    onSuccess: () => flash(true, 'Re-scoring your leads — check back in a moment.'),
    onError: () => flash(false, 'Could not start a re-score — try again.'),
  })

  const stageOf = (l: Lead) => l.manual_stage || l.stage
  const noteFor = (l: Lead) => notes[l.id] ?? l.notes ?? ''

  return (
    <div className="space-y-4">
      {msg && <p className={`text-sm font-medium ${msg.ok ? 'text-green-600' : 'text-red-600'}`}>{msg.text}</p>}

      <div className="bg-white rounded-lg border border-gray-200 shadow-sm p-5">
        <div className="flex items-start justify-between gap-3">
          <div>
            <h3 className="font-semibold text-gray-700">
              Lead pipeline{data?.hot_count ? ` — ${data.hot_count} worth acting on` : ''}
            </h3>
            <p className="text-xs text-gray-500 mt-1">
              Everyone engaging with you, scored on how well they fit what you do and how recently
              and often they show up. Rebuilt nightly. Move someone by hand or leave a note — the
              re-score won't touch it.
            </p>
          </div>
          <button
            onClick={() => refreshMutation.mutate()}
            disabled={refreshMutation.isPending}
            className="shrink-0 px-3 py-1.5 border border-gray-300 text-gray-700 rounded-lg text-xs font-semibold hover:bg-gray-50 disabled:opacity-50"
          >
            Re-score now
          </button>
        </div>
        <label className="flex items-center gap-2 mt-3 text-xs text-gray-600">
          <input type="checkbox" checked={showDismissed} onChange={(e) => setShowDismissed(e.target.checked)} />
          Show people I've removed
        </label>
      </div>

      {isLoading && <p className="text-gray-400 text-sm">Loading your pipeline…</p>}
      {!isLoading && leads.length === 0 && (
        <div className="text-center py-10 bg-white rounded-lg border border-gray-200 text-sm text-gray-500">
          No scored leads yet — they appear once people engage with your posts, or right after the
          nightly re-score.
        </div>
      )}

      <div className="grid gap-4 lg:grid-cols-2 xl:grid-cols-3">
        {STAGES.filter((s) => leads.some((l) => stageOf(l) === s)).map((stage) => (
          <div key={stage} className="space-y-3">
            <div className="flex items-center gap-2">
              <span className={`px-2 py-0.5 rounded-full text-xs font-semibold ${STAGE_COLORS[stage]}`}>
                {STAGE_LABELS[stage]}
              </span>
              <span className="text-xs text-gray-400">
                {leads.filter((l) => stageOf(l) === stage).length}
              </span>
            </div>

            {leads.filter((l) => stageOf(l) === stage).map((l) => (
              <div key={l.id} className={`bg-white rounded-lg border border-gray-200 p-4 space-y-2 ${l.dismissed ? 'opacity-60' : ''}`}>
                <div className="flex items-center justify-between gap-2">
                  {l.person_profile_url ? (
                    <a href={l.person_profile_url} target="_blank" rel="noopener noreferrer"
                      className="text-sm font-medium text-blue-700 truncate hover:underline">
                      {l.person_name || l.person_profile_url}
                    </a>
                  ) : (
                    <span className="text-sm font-medium text-gray-700 truncate">{l.person_name || 'Unknown'}</span>
                  )}
                  <span className="shrink-0 px-2 py-0.5 rounded-full text-xs font-semibold bg-amber-100 text-amber-700">
                    {l.score}
                  </span>
                </div>

                {l.reasons && <p className="text-xs text-gray-500">{l.reasons}</p>}

                {l.next_action && (
                  <p className="text-sm text-gray-700">
                    <span className="font-semibold text-gray-500">Next: </span>{l.next_action}
                  </p>
                )}

                <div className="flex flex-wrap items-center gap-2 text-xs text-gray-400">
                  <span>fit {l.icp_score}</span>
                  <span>·</span>
                  <span>engagement {l.engagement_score}</span>
                  {l.last_signal_at && (
                    <>
                      <span>·</span>
                      <span>{formatInTimezone(l.last_signal_at, userTimezone)}</span>
                    </>
                  )}
                </div>

                <div className="flex flex-wrap items-center gap-2">
                  <select
                    value={stageOf(l)}
                    onChange={(e) => updateMutation.mutate({ id: l.id, stage: e.target.value })}
                    className="border border-gray-300 rounded px-2 py-1 text-xs"
                  >
                    {STAGES.map((s) => (
                      <option key={s} value={s}>{STAGE_LABELS[s]}</option>
                    ))}
                  </select>
                  {l.manual_stage && (
                    <button onClick={() => updateMutation.mutate({ id: l.id, stage: '' })}
                      className="text-xs text-gray-500 hover:underline">
                      use scored stage
                    </button>
                  )}
                  <button
                    onClick={() => updateMutation.mutate({ id: l.id, action: l.dismissed ? 'restore' : 'dismiss' })}
                    disabled={updateMutation.isPending}
                    className="px-2 py-1 border border-gray-300 text-gray-600 rounded text-xs font-semibold hover:bg-gray-50 disabled:opacity-50">
                    {l.dismissed ? 'Restore' : 'Remove'}
                  </button>
                </div>

                <textarea value={noteFor(l)} rows={2} maxLength={NOTES_MAX}
                  onChange={(e) => setNotes({ ...notes, [l.id]: e.target.value })}
                  onBlur={() => {
                    if (noteFor(l) !== (l.notes ?? '')) updateMutation.mutate({ id: l.id, notes: noteFor(l) })
                  }}
                  placeholder="Your notes on this lead…"
                  className="w-full border border-gray-300 rounded-lg px-3 py-2 text-xs" />
              </div>
            ))}
          </div>
        ))}
      </div>
    </div>
  )
}
