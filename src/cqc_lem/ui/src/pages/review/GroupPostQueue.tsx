import { useEffect, useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import api from '../../api/client'
import { useAuth } from '../../contexts/AuthContext'
import { formatInTimezone } from '../../utils/datetime'
import type { GroupPostDraft } from '../account/types'
import { maskProps } from '../../utils/analytics'

// LinkedIn caps a post at 3000 chars (mirrors the API's _LEN_GROUP_POST).
const GROUP_POST_MAX = 3000

// The weekly group-post beat publishes on Tuesdays at 15:00 UTC (issue #932). Given a reference
// instant, return the next occurrence of that slot so the UI can show when a queued draft ships.
export function nextGroupPublishSlot(from: Date = new Date()): Date {
  const base = new Date(from)
  const candidate = new Date(
    Date.UTC(base.getUTCFullYear(), base.getUTCMonth(), base.getUTCDate(), 15, 0, 0, 0)
  )
  const day = candidate.getUTCDay()
  const daysUntilTuesday = (2 - day + 7) % 7
  candidate.setUTCDate(candidate.getUTCDate() + daysUntilTuesday)
  if (candidate.getTime() <= base.getTime()) {
    candidate.setUTCDate(candidate.getUTCDate() + 7)
  }
  return candidate
}

export default function GroupPostQueue(
  { userTimezone }: { userTimezone: string },
) {
  const { sessionToken } = useAuth()
  const qc = useQueryClient()
  const [draftText, setDraftText] = useState<string | null>(null)
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null)
  const [publishAt, setPublishAt] = useState<Date>(() => nextGroupPublishSlot())
  const mounted = useRef(false)

  useEffect(() => {
    mounted.current = true
    return () => {
      mounted.current = false
    }
  }, [])

  // Update publish time every minute to prevent staleness if component is left open
  useEffect(() => {
    const timer = window.setInterval(() => {
      setPublishAt(nextGroupPublishSlot())
    }, 60 * 1000) // Update every minute

    return () => {
      window.clearInterval(timer)
    }
  }, [])

  const { data: draft, isLoading } = useQuery<GroupPostDraft | null>({
    queryKey: ['group-post-draft', sessionToken],
    queryFn: () =>
      api
        .get(`/user/group-post-draft?session_token=${encodeURIComponent(sessionToken!)}`)
        .then((r) => (r.data.detail as GroupPostDraft | null) ?? null),
    enabled: !!sessionToken,
    staleTime: 60 * 1000,
  })

  // Adopt the server's text only until the user starts typing — a background refetch must not
  // discard an edit in progress.
  useEffect(() => {
    setDraftText((cur) => (cur === null ? draft?.content ?? null : cur))
  }, [draft])

  const draftMutation = useMutation({
    mutationFn: (body: { content?: string; status?: string }) =>
      api.put('/user/group-post-draft', { session_token: sessionToken, ...body }),
    onSuccess: async (_res, body) => {
      if (body.status) setDraftText(null)
      await qc.invalidateQueries({ queryKey: ['group-post-draft'] })
      setMsg({ ok: true, text: body.status ? 'Skipped — no group post this week.' : 'Saved.' })
      setTimeout(() => {
        if (mounted.current) setMsg(null)
      }, 3000)
    },
    onError: () => {
      setMsg({ ok: false, text: 'Could not save — try again.' })
      setTimeout(() => {
        if (mounted.current) setMsg(null)
      }, 5000)
    },
  })

  const dirty = !!draft && draftText !== null && draftText !== draft.content &&
    !!draftText.trim() && draftText.length <= GROUP_POST_MAX

  // Rendered by every branch below: a skip retires the draft, so the panel that held the button is
  // gone by the time the confirmation lands and the message has to outlive it.
  const banner = msg && (
    <p className={`text-sm font-medium ${msg.ok ? 'text-green-600' : 'text-red-600'}`}>{msg.text}</p>
  )

  if (isLoading) return <p className="text-gray-400 text-sm">Loading group post draft…</p>

  if (!draft) {
    return (
      <div className="space-y-3">
        {banner}
        <div className="flex flex-col items-center text-center py-12 px-4 bg-white rounded-lg border border-gray-200">
          <div className="text-4xl mb-4">👥</div>
          <p className="text-gray-600 text-sm mb-2 max-w-sm">No group post draft queued yet.</p>
          <p className="text-gray-400 text-xs max-w-sm">
            Group posts are drafted Sunday and published Tuesday. Enable Post on a group in Account
            settings to start the rotation.
          </p>
        </div>
      </div>
    )
  }

  return (
    <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
      {/* Summary card */}
      <div className="space-y-3">
        <div className="bg-white rounded-lg border border-blue-500 ring-1 ring-blue-500 p-4">
          <div className="flex items-center justify-between mb-1 gap-2">
            <span className="text-xs text-gray-400 truncate">
              Drafted {formatInTimezone(draft.created_at, userTimezone)}
            </span>
            <span className="px-2 py-0.5 rounded-full text-xs font-medium bg-yellow-100 text-yellow-700">
              {draft.status.toUpperCase()}
            </span>
          </div>
          <p className="text-sm font-medium text-gray-800">
            {draft.group_name || `Group ${draft.group_id}`}
          </p>
          <p className="text-xs text-gray-500 mt-1">
            Publishes {formatInTimezone(publishAt.toISOString(), userTimezone)} in the group rotation.
          </p>
        </div>
      </div>

      {/* Editor */}
      <div className="sticky top-4 self-start space-y-4 max-h-[calc(100vh-2rem)] overflow-y-auto">
        <div className="bg-white rounded-lg border border-gray-200 shadow-sm p-5 space-y-3">
          <div>
            <h3 className="text-sm font-semibold text-gray-700">
              Group post — {draft.group_name || `Group ${draft.group_id}`}
            </h3>
            <p className="text-xs text-gray-500">
              This post goes out at the next weekly group slot. Edit it, or skip it and no group post
              goes out this week.
            </p>
          </div>
          <textarea
            aria-label="Group post text"
            value={draftText ?? ''}
            onChange={(e) => setDraftText(e.target.value)}
            rows={8}
            {...maskProps('w-full border border-gray-300 rounded-lg px-3 py-2 text-sm text-gray-800 focus:outline-none focus:ring-2 focus:ring-blue-500 resize-none')}
          />
          <div className="flex items-center justify-between gap-3">
            <span className={`text-xs ${(draftText?.length ?? 0) > GROUP_POST_MAX ? 'text-red-600' : 'text-gray-500'}`}>
              {draftText?.length ?? 0}/{GROUP_POST_MAX}
            </span>
            <span className="flex items-center gap-2">
              <button type="button"
                onClick={() => draftMutation.mutate({ status: 'skipped' })}
                disabled={draftMutation.isPending}
                className="px-3 py-1.5 rounded-lg text-sm font-semibold border border-gray-300 text-gray-700 hover:bg-gray-100 disabled:opacity-50 transition-colors">
                Skip this week
              </button>
              <button type="button"
                onClick={() => draftMutation.mutate({ content: draftText as string })}
                disabled={draftMutation.isPending || !dirty}
                className="px-3 py-1.5 rounded-lg text-sm font-semibold bg-blue-600 text-white hover:bg-blue-700 disabled:opacity-50 transition-colors">
                {draftMutation.isPending ? 'Saving…' : 'Save post'}
              </button>
            </span>
          </div>
          {banner}
        </div>
      </div>
    </div>
  )
}
