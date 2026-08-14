import { useEffect, useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import api from '../../api/client'
import { useAuth } from '../../contexts/useAuth'
import { formatInTimezone } from '../../utils/datetime'
import { nextGroupPublishSlot } from '../../utils/groupPostSlot'
import type { GroupPostDraft } from '../account/types'
import { maskProps } from '../../utils/analytics'

// LinkedIn caps a post at 3000 chars (mirrors the API's _LEN_GROUP_POST).
const GROUP_POST_MAX = 3000

// The status badge's colours. A skipped draft is deliberately NOT red — nothing failed, the user
// decided, and it can be put back until the slot passes (issue #1224).
const STATUS_STYLES: Record<string, string> = {
  ready: 'bg-yellow-100 text-yellow-700',
  skipped: 'bg-gray-200 text-gray-600',
}

function errorText(err: unknown, fallback: string): string {
  const detail = (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail
  return typeof detail === 'string' ? detail : fallback
}

export default function GroupPostQueue(
  { userTimezone }: { userTimezone: string },
) {
  const { sessionToken } = useAuth()
  const qc = useQueryClient()
  // The edit in progress, once there is one. The server's text stands until then — a background
  // refetch must not discard what the user is typing.
  const [draftEdit, setDraftEdit] = useState<string | null>(null)
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null)
  const [publishAt, setPublishAt] = useState<Date>(() => nextGroupPublishSlot())
  const [mediaBusy, setMediaBusy] = useState<'upload' | 'generate' | null>(null)
  const mediaFileRef = useRef<HTMLInputElement | null>(null)
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

  const draftText = draftEdit ?? draft?.content ?? null

  function flash(ok: boolean, text: string) {
    setMsg({ ok, text })
    setTimeout(() => {
      if (mounted.current) setMsg(null)
    }, ok ? 3000 : 5000)
  }

  const draftMutation = useMutation({
    mutationFn: (body: { content?: string; status?: string; media_url?: string; remove_media?: boolean }) =>
      api.put('/user/group-post-draft', { session_token: sessionToken, ...body }),
    onSuccess: async (_res, body) => {
      if (body.status) setDraftEdit(null)
      await qc.invalidateQueries({ queryKey: ['group-post-draft'] })
      if (body.status === 'skipped') flash(true, 'Skipped — no group post this week. You can undo this until the Tuesday slot.')
      else if (body.status === 'ready') flash(true, 'Skip undone — back in the queue for this week.')
      else if (body.remove_media) flash(true, 'Media removed.')
      else if (body.media_url) flash(true, 'Media attached.')
      else flash(true, 'Saved.')
    },
    onError: (err) => flash(false, errorText(err, 'Could not save — try again.')),
  })

  // The media rides the SAME post-image surface the Content Studio uses for a scheduled post: it is
  // stored under this user's own preview dir and the group-post PUT only accepts a URL we issued
  // them, so nothing here can point the publish run at someone else's file.
  async function attachMedia(request: () => Promise<{ data: { detail: { image_url: string } } }>,
                             kind: 'upload' | 'generate') {
    if (!sessionToken) { flash(false, 'Not logged in.'); return }
    setMediaBusy(kind)
    try {
      const r = await request()
      await draftMutation.mutateAsync({ media_url: r.data.detail.image_url })
    } catch (err) {
      flash(false, errorText(err, 'Could not attach that media — try another file.'))
    } finally {
      if (mounted.current) setMediaBusy(null)
      if (mediaFileRef.current) mediaFileRef.current.value = ''
    }
  }

  function handleUpload(file: File) {
    const form = new FormData()
    form.append('session_token', sessionToken as string)
    form.append('file', file)
    return attachMedia(() => api.post('/user/post/image', form), 'upload')
  }

  function handleGenerate() {
    const text = (draftText ?? draft?.content ?? '').trim()
    if (!text) { flash(false, 'Write the post first — the image is drawn from it.'); return }
    return attachMedia(
      () => api.post('/user/post/image/generate', { session_token: sessionToken, content: text }),
      'generate')
  }

  const dirty = !!draft && draftText !== null && draftText !== draft.content &&
    !!draftText.trim() && draftText.length <= GROUP_POST_MAX
  const skipped = draft?.status === 'skipped'
  // The undo window closes at the publish slot the draft was written for (issue #1415). The server
  // refuses a restore after that, so the control is hidden rather than left to fail on a click.
  const canUndoSkip = skipped && draft?.can_undo_skip !== false
  const busy = draftMutation.isPending || mediaBusy !== null

  // Rendered by every branch below: a status change retires the panel it was clicked from, so the
  // confirmation has to outlive it.
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
        <div className={`bg-white rounded-lg border p-4 ${skipped ? 'border-gray-300' : 'border-blue-500 ring-1 ring-blue-500'}`}>
          <div className="flex items-center justify-between mb-1 gap-2">
            <span className="text-xs text-gray-400 truncate">
              Drafted {formatInTimezone(draft.created_at, userTimezone)}
            </span>
            <span className={`px-2 py-0.5 rounded-full text-xs font-medium ${STATUS_STYLES[draft.status] ?? 'bg-gray-100 text-gray-600'}`}>
              {draft.status.toUpperCase()}
            </span>
          </div>
          <p className="text-sm font-medium text-gray-800">
            {draft.group_name || `Group ${draft.group_id}`}
          </p>
          <p className="text-xs text-gray-500 mt-1">
            {skipped
              ? canUndoSkip
                ? 'Skipped — nothing goes out this week unless you undo the skip.'
                : "Skipped — this week's slot has passed, so the skip is final. A new post is drafted Sunday."
              : `Publishes ${formatInTimezone(publishAt.toISOString(), userTimezone)} in the group rotation.`}
          </p>
        </div>

        {/* Media (issue #1224) — a native image or short video out-performs text alone. */}
        <div className="bg-white rounded-lg border border-gray-200 p-4 space-y-3">
          <h3 className="text-sm font-semibold text-gray-700">Media</h3>
          {draft.media_url ? (
            draft.media_type === 'video' ? (
              <video src={draft.media_url} controls className="w-full rounded-lg border border-gray-200" />
            ) : (
              <img src={draft.media_url} alt="Group post media" className="w-full rounded-lg border border-gray-200" />
            )
          ) : (
            <p className="text-xs text-gray-500">
              Text only. Add a native image or short video — link-outs and plain text are the two
              lowest-reach formats in a group.
            </p>
          )}
          <div className="flex flex-wrap items-center gap-2">
            <input
              ref={mediaFileRef}
              type="file"
              accept="image/png,image/jpeg"
              aria-label="Group post media file"
              className="hidden"
              onChange={(e) => {
                const file = e.target.files?.[0]
                if (file) handleUpload(file)
              }}
            />
            <button type="button"
              onClick={() => mediaFileRef.current?.click()}
              disabled={busy}
              className="px-3 py-1.5 rounded-lg text-sm font-semibold border border-gray-300 text-gray-700 hover:bg-gray-100 disabled:opacity-50 transition-colors">
              {mediaBusy === 'upload' ? 'Uploading…' : 'Upload image'}
            </button>
            <button type="button"
              onClick={handleGenerate}
              disabled={busy}
              className="px-3 py-1.5 rounded-lg text-sm font-semibold border border-gray-300 text-gray-700 hover:bg-gray-100 disabled:opacity-50 transition-colors">
              {mediaBusy === 'generate' ? 'Generating…' : 'Generate with AI'}
            </button>
            {draft.media_url && (
              <button type="button"
                onClick={() => draftMutation.mutate({ remove_media: true })}
                disabled={busy}
                className="px-3 py-1.5 rounded-lg text-sm font-semibold text-red-600 hover:bg-red-50 disabled:opacity-50 transition-colors">
                Remove media
              </button>
            )}
          </div>
        </div>

        {/* The SAME list the drafting prompt is held to, served with the draft. */}
        {!!draft.best_practices?.length && (
          <div className="bg-white rounded-lg border border-gray-200 p-4">
            <h3 className="text-sm font-semibold text-gray-700 mb-2">What works in groups</h3>
            <ul className="list-disc pl-5 space-y-1 text-xs text-gray-600">
              {draft.best_practices.map((rule) => <li key={rule}>{rule}</li>)}
            </ul>
          </div>
        )}
      </div>

      {/* Editor */}
      <div className="sticky top-4 self-start space-y-4 max-h-[calc(100vh-2rem)] overflow-y-auto">
        <div className="bg-white rounded-lg border border-gray-200 shadow-sm p-5 space-y-3">
          <div>
            <h3 className="text-sm font-semibold text-gray-700">
              Group post — {draft.group_name || `Group ${draft.group_id}`}
            </h3>
            <p className="text-xs text-gray-500">
              {skipped
                ? canUndoSkip
                  ? 'This post is skipped. Undo the skip to put it back in the queue for this week.'
                  : "This post is skipped and this week's publish slot has passed, so it can no longer be put back. The next group post is drafted Sunday."
                : 'This post goes out at the next weekly group slot. Edit it, or skip it and no group post goes out this week.'}
            </p>
          </div>
          <textarea
            aria-label="Group post text"
            value={draftText ?? ''}
            onChange={(e) => setDraftEdit(e.target.value)}
            rows={8}
            {...maskProps('w-full border border-gray-300 rounded-lg px-3 py-2 text-sm text-gray-800 focus:outline-none focus:ring-2 focus:ring-blue-500 resize-none')}
          />
          <div className="flex items-center justify-between gap-3">
            <span className={`text-xs ${(draftText?.length ?? 0) > GROUP_POST_MAX ? 'text-red-600' : 'text-gray-500'}`}>
              {draftText?.length ?? 0}/{GROUP_POST_MAX}
            </span>
            <span className="flex items-center gap-2">
              {skipped ? (
                canUndoSkip && (
                  <button type="button"
                    onClick={() => draftMutation.mutate({ status: 'ready' })}
                    disabled={busy}
                    className="px-3 py-1.5 rounded-lg text-sm font-semibold border border-gray-300 text-gray-700 hover:bg-gray-100 disabled:opacity-50 transition-colors">
                    Undo skip
                  </button>
                )
              ) : (
                <button type="button"
                  onClick={() => draftMutation.mutate({ status: 'skipped' })}
                  disabled={busy}
                  className="px-3 py-1.5 rounded-lg text-sm font-semibold border border-gray-300 text-gray-700 hover:bg-gray-100 disabled:opacity-50 transition-colors">
                  Skip this week
                </button>
              )}
              <button type="button"
                onClick={() => draftMutation.mutate({ content: draftText as string })}
                disabled={busy || !dirty}
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
