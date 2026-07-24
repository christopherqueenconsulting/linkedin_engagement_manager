import { useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import api from '../../api/client'
import { useAuth } from '../../contexts/AuthContext'
import { formatInTimezone } from '../../utils/datetime'

// Inbound hot leads (issue #483): buying signals ("how much?", "can you help with X?", "DM me")
// caught in comments, replies, and DMs the automation already reads. Nothing is ever sent
// automatically — the drafted response is editable and only goes out when you approve it.
const DRAFT_MAX = 3000

interface LeadSignal {
  id: number
  source: string
  channel: string
  person_name: string | null
  person_profile_url: string | null
  snippet: string | null
  score: number
  matched_signals: string | null
  context_url: string | null
  draft_response: string | null
  status: string
  created_at: string
}

const STATUS_COLORS: Record<string, string> = {
  new: 'bg-orange-100 text-orange-700',
  approved: 'bg-green-100 text-green-700',
  sent: 'bg-blue-100 text-blue-700',
  dismissed: 'bg-gray-100 text-gray-500',
  failed: 'bg-red-100 text-red-700',
}

const SOURCE_LABELS: Record<string, string> = {
  post_comment: 'Comment on your post',
  comment_reply: 'Reply to your comment',
  dm: 'Direct message',
}

const STATUSES = ['ALL', 'new', 'approved', 'sent', 'dismissed', 'failed']

export default function LeadsInbox({ userTimezone }: { userTimezone: string }) {
  const { sessionToken } = useAuth()
  const qc = useQueryClient()
  const [filter, setFilter] = useState('new')
  const [drafts, setDrafts] = useState<Record<number, string>>({})
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null)

  const { data, isLoading } = useQuery({
    queryKey: ['lead-signals', sessionToken, filter],
    queryFn: () => {
      const p = new URLSearchParams({ session_token: sessionToken!, page: '1', page_size: '50' })
      if (filter !== 'ALL') p.set('status_filter', filter)
      return api
        .get(`/lead_signals?${p.toString()}`)
        .then((r) => r.data.detail as { signals: LeadSignal[]; total: number; new_count: number })
    },
    enabled: !!sessionToken,
  })
  const signals = data?.signals ?? []

  const flash = (ok: boolean, text: string) => { setMsg({ ok, text }); setTimeout(() => setMsg(null), 4000) }

  const actionMutation = useMutation({
    mutationFn: (v: { id: number; action?: 'approve' | 'dismiss'; draft_response?: string }) =>
      api.put('/lead_signal', {
        session_token: sessionToken,
        signal_id: v.id,
        action: v.action ?? null,
        draft_response: v.draft_response ?? null,
      }),
    onSuccess: (_r, v) => {
      qc.invalidateQueries({ queryKey: ['lead-signals'] })
      flash(true, v.action === 'approve' ? 'Approved — sending your response.'
        : v.action === 'dismiss' ? 'Dismissed.' : 'Draft saved.')
    },
    onError: () => flash(false, 'Could not update this lead — try again.'),
  })

  const draftFor = (s: LeadSignal) => drafts[s.id] ?? s.draft_response ?? ''

  return (
    <div className="space-y-4">
      {msg && <p className={`text-sm font-medium ${msg.ok ? 'text-green-600' : 'text-red-600'}`}>{msg.text}</p>}

      <div className="bg-white rounded-lg border border-gray-200 shadow-sm p-5">
        <h3 className="font-semibold text-gray-700">
          Leads inbox{data?.new_count ? ` — ${data.new_count} waiting` : ''}
        </h3>
        <p className="text-xs text-gray-500 mt-1">
          People who asked about pricing, availability, or whether you can help — spotted in comments,
          replies, and DMs. Edit the draft if you want, then approve to send it. Nothing goes out
          until you approve.
        </p>
      </div>

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

      {isLoading && <p className="text-gray-400 text-sm">Loading leads…</p>}
      {!isLoading && signals.length === 0 && (
        <div className="text-center py-10 bg-white rounded-lg border border-gray-200 text-sm text-gray-500">
          No leads here yet — they show up when someone asks about your work.
        </div>
      )}

      <div className="space-y-3">
        {signals.map((s) => (
          <div key={s.id} className="bg-white rounded-lg border border-gray-200 p-4 space-y-2">
            <div className="flex items-center justify-between gap-2">
              {s.person_profile_url ? (
                <a href={s.person_profile_url} target="_blank" rel="noopener noreferrer"
                  className="text-sm font-medium text-blue-700 truncate hover:underline">
                  {s.person_name || s.person_profile_url}
                </a>
              ) : (
                <span className="text-sm font-medium text-gray-700 truncate">{s.person_name || 'Unknown'}</span>
              )}
              <div className="flex items-center gap-2 shrink-0">
                <span className="text-xs text-gray-400">{formatInTimezone(s.created_at, userTimezone)}</span>
                <span className="px-2 py-0.5 rounded-full text-xs font-medium bg-amber-100 text-amber-700">
                  {s.score}
                </span>
                <span className={`px-2 py-0.5 rounded-full text-xs font-medium ${STATUS_COLORS[s.status] ?? 'bg-gray-100 text-gray-600'}`}>
                  {s.status.toUpperCase()}
                </span>
              </div>
            </div>

            <div className="flex flex-wrap items-center gap-2 text-xs text-gray-500">
              <span>{SOURCE_LABELS[s.source] ?? s.source}</span>
              <span>·</span>
              <span>responds by {s.channel === 'dm' ? 'DM' : 'public reply'}</span>
              {s.matched_signals && (<><span>·</span><span className="italic">{s.matched_signals}</span></>)}
              {s.context_url && (
                <a href={s.context_url} target="_blank" rel="noopener noreferrer" className="text-blue-600 hover:underline">
                  view thread
                </a>
              )}
            </div>

            {s.snippet && (
              <blockquote className="border-l-2 border-gray-200 pl-3 text-sm text-gray-700 whitespace-pre-wrap">
                {s.snippet}
              </blockquote>
            )}

            {s.status === 'new' ? (
              <>
                <textarea value={draftFor(s)} rows={3} maxLength={DRAFT_MAX}
                  onChange={(e) => setDrafts({ ...drafts, [s.id]: e.target.value })}
                  placeholder="Your response…"
                  className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm" />
                <div className="flex gap-2">
                  <button
                    onClick={() => actionMutation.mutate({ id: s.id, action: 'approve', draft_response: draftFor(s) })}
                    disabled={actionMutation.isPending || !draftFor(s).trim()}
                    className="px-3 py-1 bg-green-600 text-white rounded text-xs font-semibold hover:bg-green-700 disabled:opacity-50">
                    Approve &amp; send
                  </button>
                  <button
                    onClick={() => actionMutation.mutate({ id: s.id, draft_response: draftFor(s) })}
                    disabled={actionMutation.isPending || !draftFor(s).trim()}
                    className="px-3 py-1 border border-gray-300 text-gray-700 rounded text-xs font-semibold hover:bg-gray-50 disabled:opacity-50">
                    Save draft
                  </button>
                  <button onClick={() => actionMutation.mutate({ id: s.id, action: 'dismiss' })}
                    disabled={actionMutation.isPending}
                    className="px-3 py-1 border border-gray-300 text-gray-600 rounded text-xs font-semibold hover:bg-gray-50 disabled:opacity-50">
                    Dismiss
                  </button>
                </div>
              </>
            ) : (
              s.draft_response && (
                <p className="text-sm text-gray-700 whitespace-pre-wrap line-clamp-3">{s.draft_response}</p>
              )
            )}
          </div>
        ))}
      </div>
    </div>
  )
}
