import { useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import api from '../../api/client'
import { useAuth } from '../../contexts/useAuth'
import { formatInTimezone, zonedDateToUtcStart, zonedDateToUtcEnd } from '../../utils/datetime'
import { DATE_RANGE_FILTERS, resolveDateRange } from '../../utils/dateRange'
import type { DateRangeFilter } from '../../utils/dateRange'
import { CATCHUP_EVENTS } from '../account/types'
import { maskProps } from '../../utils/analytics'

// LinkedIn Catch-up congratulations (issue #482). LEM scans your Catch-up feed daily, scores each
// milestone against your targeting, and queues LinkedIn's own suggested congratulations (or an
// AI-written one when catchup_message_source='ai'). NOTHING is sent until you approve it (unless you
// switched Account → Catch-up approval to auto-use), and sends drip out under your catch-up + DM caps.
const MESSAGE_MAX = 1000

interface CatchupTouch {
  id: number
  profile_url: string
  person_name: string | null
  event_type: string
  event_detail: string | null
  event_period: string
  score: number
  message: string | null
  status: string
  created_at: string
}

const EVENT_LABELS: Record<string, string> = Object.fromEntries(
  CATCHUP_EVENTS.map((e) => [e.key, e.label]),
)

const STATUS_COLORS: Record<string, string> = {
  pending: 'bg-yellow-100 text-yellow-700',
  approved: 'bg-green-100 text-green-700',
  sending: 'bg-purple-100 text-purple-700',
  sent: 'bg-blue-100 text-blue-700',
  skipped: 'bg-gray-100 text-gray-500',
  failed: 'bg-red-100 text-red-700',
  canceled: 'bg-gray-100 text-gray-500',
}

// ALL first and selected by default, like every other review queue (ConnectionRequests, etc.). A
// 'pending' default made the tab read as EMPTY for anyone on catchup_touch_mode='auto_approve' —
// their drafts land 'approved' and are sent by the drip, so no row is ever 'pending' — while the
// dashboard activity feed showed the same touches going out (issue #1360).
const STATUSES = ['ALL', 'pending', 'approved', 'sending', 'sent', 'skipped', 'failed', 'canceled']

type SortBy = 'score' | 'date'
const SORT_BY_OPTIONS: { label: string; value: SortBy }[] = [
  { label: 'Fit score', value: 'score' },
  { label: 'Date drafted', value: 'date' },
]

// A touch is drafted when the milestone was SEEN, so every row's date is in the past — the Content
// Studio's forward-looking preset (it filters scheduled_time) would always return nothing here.
const CATCHUP_DATE_RANGES = DATE_RANGE_FILTERS.filter((d) => d.value !== 'next30days')

export default function CatchupTouches({ userTimezone }: { userTimezone: string }) {
  const { sessionToken } = useAuth()
  const qc = useQueryClient()
  const [filter, setFilter] = useState('ALL')
  const [sortBy, setSortBy] = useState<SortBy>('score')
  const [sortOrder, setSortOrder] = useState<'asc' | 'desc'>('desc')
  const [dateRange, setDateRange] = useState<DateRangeFilter>('ALL')
  const [customStartDate, setCustomStartDate] = useState('')
  const [customEndDate, setCustomEndDate] = useState('')
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null)
  // Per-touch message edits (keyed by id) so each card edits independently.
  const [edits, setEdits] = useState<Record<number, string>>({})

  const { start: filterStartDate, end: filterEndDate } = resolveDateRange(
    dateRange, userTimezone, customStartDate, customEndDate
  )

  const { data, isLoading } = useQuery({
    queryKey: ['catchup-touches', sessionToken, filter, sortBy, sortOrder,
               dateRange, filterStartDate, filterEndDate],
    queryFn: () => {
      const p = new URLSearchParams({
        session_token: sessionToken!, page: '1', page_size: '50',
        sort_by: sortBy, sort_order: sortOrder,
      })
      if (filter !== 'ALL') p.set('status_filter', filter)
      if (filterStartDate) p.set('start_date', zonedDateToUtcStart(filterStartDate, userTimezone) ?? '')
      if (filterEndDate) p.set('end_date', zonedDateToUtcEnd(filterEndDate, userTimezone) ?? '')
      return api
        .get(`/catchup/touches?${p.toString()}`)
        .then((r) => r.data.detail as { touches: CatchupTouch[]; total: number })
    },
    enabled: !!sessionToken,
  })
  const touches = data?.touches ?? []
  // The request is page 1 of 50 with no pager, so on ALL a big archive of sent/skipped rows can
  // outrank the few PENDING drafts that still need a decision. Say so out loud rather than let the
  // list read as complete (the same silence as issue #1360) — and name the sort that decided WHICH
  // 50 arrived, since a date sort hides a different 50 than the score sort does (issue #1464).
  const total = data?.total ?? 0
  const hiddenByPaging = Math.max(0, total - touches.length)
  const sortSummary = sortBy === 'date'
    ? (sortOrder === 'desc' ? 'most recent' : 'oldest')
    : (sortOrder === 'desc' ? 'highest-scoring' : 'lowest-scoring')
  // One string, not interpolated JSX: an empty queue is the state a test (and a reader) checks by
  // its exact sentence, and React would otherwise split it across text nodes.
  const emptyMessage = `No catch-up touches ${filter === 'ALL' ? 'yet' : `with status ${filter}`}`
    + `${dateRange === 'ALL' ? '' : ' in this date range'}.`

  const flash = (ok: boolean, text: string) => { setMsg({ ok, text }); setTimeout(() => setMsg(null), 4000) }
  const invalidate = () => qc.invalidateQueries({ queryKey: ['catchup-touches'] })

  const saveMutation = useMutation({
    mutationFn: (v: { id: number; message: string }) =>
      api.put('/catchup/touch', { session_token: sessionToken, touch_id: v.id, message: v.message }),
    onSuccess: (_r, v) => {
      invalidate(); setEdits((e) => { const n = { ...e }; delete n[v.id]; return n }); flash(true, 'Message saved.')
    },
    onError: () => flash(false, 'Could not save the message.'),
  })

  // Approving sends, so it carries the textarea's current value — otherwise an edit the user never
  // clicked "Save message" on would be silently dropped and the OLD draft would go out.
  const actionMutation = useMutation({
    mutationFn: (v: { id: number; action: 'approve' | 'cancel'; message?: string }) =>
      api.put('/catchup/touch', {
        session_token: sessionToken, touch_id: v.id, action: v.action,
        ...(v.message !== undefined ? { message: v.message } : {}),
      }),
    onSuccess: (_r, v) => {
      invalidate(); setEdits((e) => { const n = { ...e }; delete n[v.id]; return n })
    },
    onError: () => flash(false, 'Could not update this touch.'),
  })

  return (
    <div className="space-y-4">
      {msg && <p className={`text-sm font-medium ${msg.ok ? 'text-green-600' : 'text-red-600'}`}>{msg.text}</p>}

      <div className="bg-white rounded-lg border border-gray-200 shadow-sm p-5 space-y-1">
        <h3 className="font-semibold text-gray-700">Catch-up congratulations</h3>
        <p className="text-xs text-gray-500">
          LEM scans your LinkedIn Catch-up feed daily for network milestones — new jobs, promotions,
          work anniversaries — scores them against your targeting, and queues LinkedIn's own suggested
          congratulations for each one (switch to an AI-written version in Account → Catch-up message).
          <span className="font-semibold"> Nothing sends until you approve it</span> — unless
          Account → Catch-up approval is set to "Auto-use", in which case drafts land already
          approved and this queue shows them as APPROVED/SENT rather than PENDING. Each milestone is
          messaged at most once, and approved messages drip out under your daily catch-up and DM caps
          (Account → Engagement).
        </p>
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

      {/* Sort + date-range filter (issue #1464) */}
      <div className="flex items-center gap-3 flex-wrap">
        <label className="flex items-center gap-1.5 text-xs text-gray-600">
          <span>Sort by</span>
          <select
            value={sortBy}
            onChange={(e) => setSortBy(e.target.value as SortBy)}
            aria-label="Sort catch-up touches by"
            className="border border-gray-300 rounded-lg px-2 py-1.5 text-xs focus:outline-none focus:ring-1 focus:ring-blue-500"
          >
            {SORT_BY_OPTIONS.map((o) => (
              <option key={o.value} value={o.value}>{o.label}</option>
            ))}
          </select>
        </label>
        <button
          onClick={() => setSortOrder((o) => (o === 'asc' ? 'desc' : 'asc'))}
          className="flex items-center gap-1 text-gray-600 border border-gray-300 rounded-lg px-3 py-1.5 hover:border-blue-400 transition-colors text-xs font-medium"
        >
          {sortBy === 'date'
            ? (sortOrder === 'asc' ? '↑ Oldest first' : '↓ Newest first')
            : (sortOrder === 'asc' ? '↑ Lowest score first' : '↓ Highest score first')}
        </button>
        <label className="flex items-center gap-1.5 text-xs text-gray-600">
          <span>Date</span>
          <select
            value={dateRange}
            onChange={(e) => setDateRange(e.target.value as DateRangeFilter)}
            aria-label="Date range preset"
            className="border border-gray-300 rounded-lg px-2 py-1.5 text-xs focus:outline-none focus:ring-1 focus:ring-blue-500"
          >
            {CATCHUP_DATE_RANGES.map((d) => (
              <option key={d.value} value={d.value}>{d.label}</option>
            ))}
          </select>
        </label>
        {dateRange === 'custom' && (
          <div className="flex items-center gap-1.5 text-xs text-gray-600">
            <input
              type="date"
              value={customStartDate}
              onChange={(e) => setCustomStartDate(e.target.value)}
              aria-label="Custom start date"
              className="border border-gray-300 rounded-lg px-2 py-1.5 text-xs focus:outline-none focus:ring-1 focus:ring-blue-500"
            />
            <span>to</span>
            <input
              type="date"
              value={customEndDate}
              onChange={(e) => setCustomEndDate(e.target.value)}
              aria-label="Custom end date"
              className="border border-gray-300 rounded-lg px-2 py-1.5 text-xs focus:outline-none focus:ring-1 focus:ring-blue-500"
            />
          </div>
        )}
      </div>

      {!isLoading && hiddenByPaging > 0 && (
        <p className="text-xs text-gray-500">
          Showing the {touches.length} {sortSummary} of {total} touches — {hiddenByPaging} not
          listed. Pick a status or date range above to narrow the list.
        </p>
      )}

      {isLoading && <p className="text-gray-400 text-sm">Loading catch-up touches…</p>}
      {!isLoading && touches.length === 0 && (
        <div className="text-center py-10 bg-white rounded-lg border border-gray-200 text-sm text-gray-500 space-y-2">
          <p>{emptyMessage}</p>
          {filter !== 'ALL' && (
            <button onClick={() => setFilter('ALL')}
              className="px-3 py-1 border border-gray-300 rounded text-xs font-semibold text-gray-600 hover:bg-gray-50">
              Show all statuses
            </button>
          )}
          {dateRange !== 'ALL' && (
            <button onClick={() => setDateRange('ALL')}
              className="px-3 py-1 border border-gray-300 rounded text-xs font-semibold text-gray-600 hover:bg-gray-50">
              Show all dates
            </button>
          )}
        </div>
      )}

      <div className="space-y-3">
        {touches.map((t) => {
          const editing = edits[t.id] !== undefined
          const messageValue = editing ? edits[t.id] : (t.message ?? '')
          const editable = ['pending', 'approved', 'failed'].includes(t.status)
          return (
            <div key={t.id} className="bg-white rounded-lg border border-gray-200 p-4 space-y-2">
              <div className="flex items-center justify-between gap-2">
                <a href={t.profile_url} target="_blank" rel="noopener noreferrer"
                  className="text-sm font-medium text-blue-700 truncate hover:underline">
                  {t.person_name || t.profile_url}
                </a>
                <div className="flex items-center gap-2 shrink-0">
                  <span className="px-2 py-0.5 rounded-full text-xs font-medium bg-indigo-100 text-indigo-700">
                    {EVENT_LABELS[t.event_type] ?? t.event_type}
                  </span>
                  <span className="text-xs text-gray-400" title="Fit score">{t.score}</span>
                  <span className="text-xs text-gray-400">{formatInTimezone(t.created_at, userTimezone)}</span>
                  <span className={`px-2 py-0.5 rounded-full text-xs font-medium ${STATUS_COLORS[t.status] ?? 'bg-gray-100 text-gray-600'}`}>
                    {t.status.toUpperCase()}
                  </span>
                </div>
              </div>
              {t.event_detail && <p className="text-xs text-gray-400 line-clamp-2">{t.event_detail}</p>}

              {editable ? (
                <>
                  <textarea value={messageValue} rows={3} maxLength={MESSAGE_MAX}
                    onChange={(e) => setEdits((prev) => ({ ...prev, [t.id]: e.target.value }))}
                    placeholder="Your congratulations message…"
                    {...maskProps('w-full border border-gray-300 rounded-lg px-3 py-2 text-sm')} />
                  <div className="flex gap-2 flex-wrap">
                    <button onClick={() => saveMutation.mutate({ id: t.id, message: messageValue })}
                      disabled={!editing || saveMutation.isPending}
                      className="px-3 py-1 border border-gray-300 text-gray-600 rounded text-xs font-semibold hover:bg-gray-50 disabled:opacity-50">
                      Save message
                    </button>
                    {['pending', 'failed'].includes(t.status) && (
                      <button
                        onClick={() => actionMutation.mutate({ id: t.id, action: 'approve', message: messageValue.trim() })}
                        disabled={actionMutation.isPending || !messageValue.trim()}
                        title={!messageValue.trim() ? 'Write a message before approving' : undefined}
                        className="px-3 py-1 bg-green-600 text-white rounded text-xs font-semibold hover:bg-green-700 disabled:opacity-50">
                        {t.status === 'pending' ? 'Approve & send' : 'Retry'}
                      </button>
                    )}
                    <button onClick={() => actionMutation.mutate({ id: t.id, action: 'cancel' })}
                      disabled={actionMutation.isPending}
                      className="px-3 py-1 border border-gray-300 text-gray-600 rounded text-xs font-semibold hover:bg-gray-50 disabled:opacity-50">
                      Cancel
                    </button>
                  </div>
                </>
              ) : (
                t.message && <p className="text-sm text-gray-700 whitespace-pre-wrap line-clamp-3">{t.message}</p>
              )}
            </div>
          )
        })}
      </div>
    </div>
  )
}
