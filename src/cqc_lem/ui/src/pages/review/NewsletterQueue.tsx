import { useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import api from '../../api/client'
import { useAuth } from '../../contexts/AuthContext'
import NewsletterArticlePreview from '../../components/NewsletterArticlePreview'
import { formatInTimezone } from '../../utils/datetime'
import type { NewsletterDraft, NewsletterEdition } from '../account/types'

const STATUS_COLORS: Record<string, string> = {
  draft: 'bg-yellow-100 text-yellow-700',
  approved: 'bg-green-100 text-green-700',
}

export default function NewsletterQueue({ userTimezone }: { userTimezone: string }) {
  const { user, sessionToken } = useAuth()
  const qc = useQueryClient()
  const [selectedId, setSelectedId] = useState<number | null>(null)
  const [draftEdit, setDraftEdit] = useState<NewsletterEdition | null>(null)
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null)

  const { data, isLoading } = useQuery({
    queryKey: ['newsletter-queue', sessionToken],
    queryFn: () =>
      api
        .get(`/user/newsletter-draft?session_token=${encodeURIComponent(sessionToken!)}`)
        .then((r) => r.data.detail as NewsletterDraft),
    enabled: !!sessionToken,
    staleTime: 30 * 1000,
  })

  const editions = data?.editions ?? []

  // Seed the editor only when the current selection is GONE (initial load, or the selected draft was
  // approved/skipped away) — default to the soonest draft. When the selection is still present we
  // leave draftEdit alone so a background refetch can't wipe in-progress edits.
  useEffect(() => {
    if (editions.length === 0) {
      setSelectedId(null)
      setDraftEdit(null)
      return
    }
    if (!editions.some((e) => e.id === selectedId)) {
      setSelectedId(editions[0].id)
      setDraftEdit({ ...editions[0] })
    }
  }, [data, selectedId])

  const setDe = (patch: Partial<NewsletterEdition>) => setDraftEdit((p) => (p ? { ...p, ...patch } : p))

  const draftMutation = useMutation({
    mutationFn: (action: 'save' | 'approve' | 'skip') =>
      api.put('/user/newsletter-draft', {
        session_token: sessionToken,
        edition_id: draftEdit!.id,
        title: draftEdit!.title,
        subtitle: draftEdit!.subtitle,
        body: draftEdit!.body,
        action,
      }),
    onSuccess: (_res, action) => {
      qc.invalidateQueries({ queryKey: ['newsletter-queue'] })
      // Approving/skipping removes the edition from the queue — move selection off it.
      if (action !== 'save') setSelectedId(null)
      setMsg({ ok: true, text: action === 'skip' ? 'Skipped.' : action === 'approve' ? 'Approved.' : 'Saved.' })
      setTimeout(() => setMsg(null), 3000)
    },
    onError: () => {
      setMsg({ ok: false, text: 'Could not save — try again.' })
      setTimeout(() => setMsg(null), 5000)
    },
  })

  function selectEdition(e: NewsletterEdition) {
    setSelectedId(e.id)
    setDraftEdit({ ...e })
  }

  if (isLoading) return <p className="text-gray-400 text-sm">Loading drafts…</p>

  if (editions.length === 0) {
    return (
      <div className="flex flex-col items-center text-center py-12 px-4 bg-white rounded-lg border border-gray-200">
        <div className="text-4xl mb-4">📰</div>
        <p className="text-gray-600 text-sm mb-2 max-w-sm">No newsletter drafts queued yet.</p>
        <p className="text-gray-400 text-xs max-w-sm">
          Enable your newsletter and set how many drafts to keep ready on the Account page — drafts will
          appear here for review before they auto-publish.
        </p>
      </div>
    )
  }

  return (
    <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
      {/* Queue list */}
      <div className="space-y-3">
        {data?.next_publish && (
          <p className="text-xs text-gray-400">
            Next auto-generated slot: {formatInTimezone(data.next_publish, userTimezone)}
          </p>
        )}
        {editions.map((e) => (
          <div
            key={e.id}
            onClick={() => selectEdition(e)}
            className={`bg-white rounded-lg border p-4 cursor-pointer hover:border-blue-400 transition-colors ${
              selectedId === e.id ? 'border-blue-500 ring-1 ring-blue-500' : 'border-gray-200'
            }`}
          >
            <div className="flex items-center justify-between mb-1 gap-2">
              <span className="text-xs text-gray-400 truncate">
                {e.scheduled_for ? formatInTimezone(e.scheduled_for, userTimezone) : 'Unscheduled'}
              </span>
              <span className={`px-2 py-0.5 rounded-full text-xs font-medium ${STATUS_COLORS[e.status] ?? 'bg-gray-100 text-gray-600'}`}>
                {e.status.toUpperCase()}
              </span>
            </div>
            <p className="text-sm font-medium text-gray-800 truncate">{e.title || 'Untitled edition'}</p>
            {e.subtitle && <p className="text-xs text-gray-500 line-clamp-1">{e.subtitle}</p>}
          </div>
        ))}
      </div>

      {/* Editor + preview */}
      {draftEdit && (
        <div className="sticky top-4 self-start space-y-4 max-h-[calc(100vh-2rem)] overflow-y-auto">
          <div className="bg-white rounded-lg border border-gray-200 shadow-sm p-5 space-y-3">
            <div>
              <h3 className="text-sm font-semibold text-gray-700">Review draft</h3>
              {draftEdit.scheduled_for && (
                <p className="text-xs text-gray-500">
                  Auto-publishes {formatInTimezone(draftEdit.scheduled_for, userTimezone)} unless you skip it.
                </p>
              )}
            </div>
            <div>
              <label className="block text-sm font-medium text-gray-700 mb-1">Title</label>
              <input type="text" value={draftEdit.title || ''} onChange={(e) => setDe({ title: e.target.value })}
                className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500" />
            </div>
            <div>
              <label className="block text-sm font-medium text-gray-700 mb-1">Subtitle</label>
              <input type="text" value={draftEdit.subtitle || ''} onChange={(e) => setDe({ subtitle: e.target.value })}
                className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500" />
            </div>
            <div>
              <label className="block text-sm font-medium text-gray-700 mb-1">Body</label>
              <textarea value={draftEdit.body || ''} onChange={(e) => setDe({ body: e.target.value })} rows={10}
                className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm font-mono focus:outline-none focus:ring-2 focus:ring-blue-500" />
            </div>
            {msg && <p className={`text-sm font-medium ${msg.ok ? 'text-green-600' : 'text-red-600'}`}>{msg.text}</p>}
            <div className="grid grid-cols-3 gap-2">
              <button type="button" onClick={() => draftMutation.mutate('save')} disabled={draftMutation.isPending}
                className="bg-gray-100 text-gray-700 py-2 rounded-lg text-sm font-semibold hover:bg-gray-200 disabled:opacity-50 transition-colors">
                Save
              </button>
              <button type="button" onClick={() => draftMutation.mutate('approve')} disabled={draftMutation.isPending}
                className="bg-blue-600 text-white py-2 rounded-lg text-sm font-semibold hover:bg-blue-700 disabled:opacity-50 transition-colors">
                Approve
              </button>
              <button type="button" onClick={() => draftMutation.mutate('skip')} disabled={draftMutation.isPending}
                className="bg-white border border-gray-300 text-gray-700 py-2 rounded-lg text-sm font-semibold hover:bg-gray-50 disabled:opacity-50 transition-colors">
                Skip
              </button>
            </div>
          </div>

          <NewsletterArticlePreview
            title={draftEdit.title}
            subtitle={draftEdit.subtitle}
            body={draftEdit.body}
            author={(user?.email ?? 'You').split('@')[0]}
          />
        </div>
      )}
    </div>
  )
}
