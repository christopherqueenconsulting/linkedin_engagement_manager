import { useCallback, useEffect, useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import api from '../../api/client'
import { useAuth } from '../../contexts/useAuth'
import { maskProps } from '../../utils/analytics'
import {
  FLAG_LABELS,
  UNDO_LANES,
  ageTone,
  bulkTargets,
  groupOf,
  highlightSegments,
  isSweepable,
} from './reviewHighlights'
import type { ReviewGroup, ReviewItem } from './reviewHighlights'

// Mirrors BULK_CAP in utilities/review_actions.py.
const BULK_CAP = 100
const FUNNEL_LANE = 'outreach_funnel_targets'

const GROUPS: { key: ReviewGroup; label: string }[] = [
  { key: 'clean', label: 'No flags' },
  { key: 'check', label: 'Check these' },
  { key: 'held', label: 'Held' },
]

const AGE_CLASS = {
  red: 'bg-danger/10 text-danger',
  amber: 'bg-warning/10 text-warning',
  none: 'bg-hairline text-fg-muted',
}

const ERROR_TEXT: Record<string, string> = {
  not_waiting_for_review: 'already handled',
  empty_message: 'draft is empty',
  not_found: 'not found',
  lane_not_actionable: 'edit this one by hand',
  write_failed: 'could not save',
  server_unreachable: 'server unreachable',
  agent_created_approve_individually: 'agent-created — approve it on its own',
}

interface BulkResult {
  lane: string
  id: number
  ok: boolean
  error: string | null
}

type Action = 'approve' | 'cancel'
const keyOf = (i: Pick<ReviewItem, 'lane' | 'id'>) => `${i.lane}:${i.id}`

function ageLabel(hours: number | null): string {
  if (hours === null) return 'age unknown'
  return hours < 1 ? '<1h' : hours < 48 ? `${Math.round(hours)}h` : `${Math.round(hours / 24)}d`
}

function Highlighted({ item }: { item: ReviewItem }) {
  return (
    <>
      {highlightSegments(item.draft_text, item.risk_flags).map((s, n) =>
        s.flagged ? (
          <mark key={n} data-flagged="true" className="bg-warning/20 text-fg rounded px-0.5">
            {s.text}
          </mark>
        ) : (
          <span key={n}>{s.text}</span>
        ),
      )}
    </>
  )
}

export default function DailyReview() {
  const { sessionToken } = useAuth()
  const qc = useQueryClient()
  const [cursor, setCursor] = useState(0)
  const [expanded, setExpanded] = useState<Set<string>>(new Set())
  const [selected, setSelected] = useState<Set<string>>(new Set())
  const [confirmCancel, setConfirmCancel] = useState<ReviewItem | null>(null)
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null)
  const [undoable, setUndoable] = useState<ReviewItem[]>([])

  const { data: items = [], isLoading, isError } = useQuery<ReviewItem[]>({
    queryKey: ['review-queue', sessionToken],
    queryFn: () =>
      api
        .get(`/review/queue?session_token=${encodeURIComponent(sessionToken!)}`)
        .then((r) => r.data.detail as ReviewItem[]),
    enabled: !!sessionToken,
    staleTime: 30 * 1000,
  })

  // Same order as the groups render in, so j/k walk what the eye sees.
  const ordered = useMemo(
    () => GROUPS.flatMap((g) => items.filter((i) => groupOf(i) === g.key)),
    [items],
  )
  const sweepable = useMemo(() => items.filter(isSweepable), [items])
  const safeCursor = Math.min(cursor, Math.max(ordered.length - 1, 0))
  const current: ReviewItem | undefined = ordered[safeCursor]

  const bulk = useMutation({
    mutationFn: async ({ targets, action }: { targets: ReviewItem[]; action: Action }) => {
      const results: BulkResult[] = []
      for (let n = 0; n < targets.length; n += BULK_CAP) {
        const chunk = targets.slice(n, n + BULK_CAP)
        try {
          const r = await api.post('/review/bulk', {
            session_token: sessionToken,
            action,
            items: chunk.map((i) => ({ lane: i.lane, id: i.id })),
          })
          results.push(...(r.data.detail as BulkResult[]))
        } catch (err) {
          // A later chunk failing must not erase the earlier chunks that already landed.
          if (n === 0) throw err
          results.push(...chunk.map((i) => ({ lane: i.lane, id: i.id, ok: false, error: 'server_unreachable' })))
        }
      }
      return results
    },
    onSuccess: (results, { targets, action }) => {
      const ok = results.filter((r) => r.ok)
      const failed = results.filter((r) => !r.ok)
      const verb = action === 'approve' ? 'Approved' : 'Canceled'
      const reasons = failed.map((f) => ERROR_TEXT[f.error ?? ''] ?? f.error).join(', ')
      setMsg({
        ok: failed.length === 0,
        text: `${verb} ${ok.length}${failed.length ? `; ${failed.length} not done (${reasons})` : ''}.`,
      })
      const okKeys = new Set(ok.map(keyOf))
      setUndoable(
        action === 'approve'
          ? targets.filter((t) => okKeys.has(keyOf(t)) && UNDO_LANES.includes(t.lane))
          : [],
      )
      setSelected(new Set())
      setConfirmCancel(null)
      qc.invalidateQueries({ queryKey: ['review-queue'] })
      qc.invalidateQueries({ queryKey: ['review-summary'] })
    },
    onError: () => setMsg({ ok: false, text: 'Could not reach the server — nothing was changed.' }),
  })

  const run = useCallback(
    (targets: ReviewItem[], action: Action) => {
      const allowed = bulkTargets(targets, action).filter((t) => t.allowed_actions.includes(action))
      if (!allowed.length || bulk.isPending) return
      // Cancelling a funnel target aborts the whole funnel, so it is never one keystroke away.
      if (action === 'cancel' && allowed.some((t) => t.lane === FUNNEL_LANE)) {
        setConfirmCancel(allowed.find((t) => t.lane === FUNNEL_LANE)!)
        return
      }
      bulk.mutate({ targets: allowed, action })
    },
    [bulk],
  )

  const toggle = (set: Set<string>, k: string) => {
    const next = new Set(set)
    if (next.has(k)) next.delete(k)
    else next.add(k)
    return next
  }

  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      const t = e.target as HTMLElement | null
      if (t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.isContentEditable)) return
      if (e.ctrlKey || e.metaKey || e.altKey) return
      if (e.key === 'A' && e.shiftKey) {
        e.preventDefault()
        run(sweepable, 'approve')
        return
      }
      if (e.shiftKey) return
      switch (e.key) {
        case 'j':
          setCursor((c) => Math.min(c + 1, ordered.length - 1))
          break
        case 'k':
          setCursor((c) => Math.max(c - 1, 0))
          break
        case 'a':
          if (current) run([current], 'approve')
          break
        case 'x':
          if (current) run([current], 'cancel')
          break
        case 'e':
          if (current) setExpanded((s) => toggle(s, keyOf(current)))
          break
        case ' ':
          if (current) {
            e.preventDefault()
            setSelected((s) => toggle(s, keyOf(current)))
          }
          break
        default:
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [ordered.length, current, sweepable, run])

  if (isLoading) return <p className="text-sm text-fg-muted">Loading your review queue…</p>
  if (isError) return <p className="text-sm text-danger">Could not load the review queue.</p>

  const selectedItems = items.filter((i) => selected.has(keyOf(i)))

  return (
    <section aria-label="Daily review" className="space-y-4">
      <div className="flex flex-wrap items-center gap-3">
        <p className="text-sm text-fg-muted">
          {items.length === 0 ? 'Nothing is waiting on you.' : `${items.length} waiting, oldest first.`}
        </p>
        {sweepable.length > 0 && (
          <button
            onClick={() => run(sweepable, 'approve')}
            disabled={bulk.isPending}
            className="min-h-11 rounded-panel bg-accent px-3 text-sm font-semibold text-white disabled:opacity-50"
          >
            Approve all {sweepable.length} with no flags
          </button>
        )}
        {selectedItems.length > 0 && (
          <button
            onClick={() => run(selectedItems, 'approve')}
            disabled={bulk.isPending}
            className="min-h-11 rounded-panel border border-hairline px-3 text-sm font-medium"
          >
            Approve selected ({selectedItems.length})
          </button>
        )}
        <span className="ml-auto text-xs text-fg-muted">
          j/k move · a approve · x cancel · e expand · space select · shift+a approve all clean
        </span>
      </div>

      {msg && (
        <div
          role="status"
          className={`rounded-panel border px-3 py-2 text-sm ${
            msg.ok ? 'border-success/25 bg-success/10 text-success' : 'border-danger/25 bg-danger/10 text-danger'
          }`}
        >
          {msg.text}
          {undoable.length > 0 && (
            <button
              onClick={() => {
                bulk.mutate({ targets: undoable.map((u) => ({ ...u, allowed_actions: ['cancel'] })), action: 'cancel' })
              }}
              className="ml-3 underline font-medium"
            >
              Undo {undoable.length} scheduled {undoable.length === 1 ? 'DM' : 'DMs'}
            </button>
          )}
        </div>
      )}

      {confirmCancel && (
        <div role="alertdialog" aria-label="Confirm funnel cancel" className="rounded-panel border border-danger/25 bg-danger/5 px-3 py-2 text-sm">
          Canceling “{confirmCancel.summary}” aborts the whole funnel for this person, not just this
          stage.
          <button
            onClick={() => bulk.mutate({ targets: [confirmCancel], action: 'cancel' })}
            className="ml-3 font-semibold text-danger underline"
          >
            Abort funnel
          </button>
          <button onClick={() => setConfirmCancel(null)} className="ml-3 underline">
            Keep it
          </button>
        </div>
      )}

      {GROUPS.map((g) => {
        const rows = items.filter((i) => groupOf(i) === g.key)
        if (!rows.length) return null
        return (
          <div key={g.key} role="group" aria-label={g.label} className="space-y-2">
            <h3 className="text-sm font-semibold text-fg">
              {g.label} <span className="font-normal text-fg-muted">({rows.length})</span>
            </h3>
            <ul className="space-y-2">
              {rows.map((item) => {
                const k = keyOf(item)
                const open = expanded.has(k)
                const isCurrent = current && keyOf(current) === k
                const tone = ageTone(item.age_hours)
                return (
                  <li
                    key={k}
                    data-testid={`review-item-${k}`}
                    aria-current={isCurrent ? 'true' : undefined}
                    className={`rounded-panel border p-3 ${isCurrent ? 'border-accent' : 'border-hairline'}`}
                  >
                    <div className="flex flex-wrap items-center gap-2">
                      <input
                        type="checkbox"
                        aria-label={`Select ${item.summary}`}
                        checked={selected.has(k)}
                        onChange={() => setSelected((s) => toggle(s, k))}
                      />
                      <span className="text-sm font-medium text-fg">{item.summary}</span>
                      {item.agent_created && (
                        <span
                          data-testid="agent-created-badge"
                          title="Queued by your coding agent. Approve it on its own."
                          className="rounded bg-accent/10 px-1.5 py-0.5 text-xs font-medium text-accent"
                        >
                          Agent-created
                        </span>
                      )}
                      <span
                        data-testid="age-badge"
                        data-tone={tone}
                        className={`rounded px-1.5 py-0.5 text-xs ${AGE_CLASS[tone]}`}
                      >
                        {ageLabel(item.age_hours)}
                      </span>
                      {item.risk_flags.map((f) => (
                        <span key={f} className="rounded bg-warning/10 px-1.5 py-0.5 text-xs text-warning">
                          {FLAG_LABELS[f] ?? f}
                        </span>
                      ))}
                    </div>
                    {item.hold_reason && (
                      <p className="mt-1 text-xs text-danger">Held: {item.hold_reason}</p>
                    )}
                    <p
                      {...maskProps(`mt-2 text-sm text-fg whitespace-pre-wrap ${open ? '' : 'line-clamp-2'}`)}
                    >
                      <Highlighted item={item} />
                    </p>
                    <div className="mt-2 flex flex-wrap gap-2 text-sm">
                      <button onClick={() => setExpanded((s) => toggle(s, k))} className="underline">
                        {open ? 'Collapse' : 'Expand'}
                      </button>
                      <Link to={item.edit_href} className="underline">
                        Edit
                      </Link>
                      {item.allowed_actions.includes('approve') && (
                        <button
                          onClick={() => run([item], 'approve')}
                          disabled={bulk.isPending}
                          className="font-semibold text-accent underline"
                        >
                          Approve
                        </button>
                      )}
                      {item.allowed_actions.includes('cancel') && (
                        <button
                          onClick={() => run([item], 'cancel')}
                          disabled={bulk.isPending}
                          className="text-danger underline"
                        >
                          Cancel
                        </button>
                      )}
                    </div>
                  </li>
                )
              })}
            </ul>
          </div>
        )
      })}
    </section>
  )
}
