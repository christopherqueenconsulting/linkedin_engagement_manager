import { useEffect, useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import api from '../../api/client'
import { useAuth } from '../../contexts/useAuth'
import NewsletterArticlePreview from '../../components/NewsletterArticlePreview'
import { formatInTimezone, toZonedInputValue, zonedInputToUtcIso } from '../../utils/datetime'
import type { NewsletterDraft, NewsletterEdition } from '../account/types'
import { maskProps } from '../../utils/analytics'

const STATUS_COLORS: Record<string, string> = {
  draft: 'bg-yellow-100 text-yellow-700',
  approved: 'bg-green-100 text-green-700',
}

// e.g. "case_study" -> "Case Study" for the format/hook badges.
const prettyKey = (k: string) => k.replace(/_/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase())

export default function NewsletterQueue(
  { userTimezone, timezoneResolved }: { userTimezone: string; timezoneResolved: boolean },
) {
  const { user, sessionToken } = useAuth()
  const qc = useQueryClient()
  const [selectedId, setSelectedId] = useState<number | null>(null)
  // Only the fields the user has changed, and the edition they belong to. Keyed by id so a
  // background refetch — or switching to another draft — can never apply an edit to the wrong one.
  const [edits, setEdits] = useState<{ id: number; patch: Partial<NewsletterEdition> } | null>(null)
  const [guidance, setGuidance] = useState('')
  // After a regenerate lands, force the editor to re-seed from the freshly fetched edition (same id,
  // new content) — the normal seed effect only fires when the selection is GONE.
  const [reseedId, setReseedId] = useState<number | null>(null)
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null)
  // Set while an AI cover is being generated — the queue is polled until that edition has one.
  const [coverWaitId, setCoverWaitId] = useState<number | null>(null)
  // The cover URL that edition carried when generation started — the poll waits for a DIFFERENT one.
  const [coverWaitFrom, setCoverWaitFrom] = useState<string | null>(null)
  const coverFileRef = useRef<HTMLInputElement>(null)

  const { data, isLoading, refetch } = useQuery({
    queryKey: ['newsletter-queue', sessionToken],
    queryFn: () =>
      api
        .get(`/user/newsletter-draft?session_token=${encodeURIComponent(sessionToken!)}`)
        .then((r) => r.data.detail as NewsletterDraft),
    enabled: !!sessionToken,
    staleTime: 30 * 1000,
  })

  const editions = data?.editions ?? []

  // The editor shows the selected edition — or the soonest one when the selection is gone (initial
  // load, or the draft was approved/skipped away) — with the user's own edits laid over it. Derived
  // at render, so nothing has to be seeded and a refetch cannot wipe an edit in progress.
  const selected = editions.find((e) => e.id === selectedId) ?? editions[0] ?? null
  const draftEdit: NewsletterEdition | null = selected
    ? { ...selected, ...(edits?.id === selected.id ? edits.patch : {}) }
    : null

  const setDe = (patch: Partial<NewsletterEdition>) =>
    setEdits((p) =>
      selected
        ? { id: selected.id, patch: { ...(p?.id === selected.id ? p.patch : {}), ...patch } }
        : p)

  const draftMutation = useMutation({
    mutationFn: (action: 'save' | 'approve' | 'skip') =>
      api.put('/user/newsletter-draft', {
        session_token: sessionToken,
        edition_id: draftEdit!.id,
        title: draftEdit!.title,
        subtitle: draftEdit!.subtitle,
        body: draftEdit!.body,
        scheduled_datetime: draftEdit!.scheduled_for || null,
        action,
      }),
    onSuccess: (_res, action) => {
      qc.invalidateQueries({ queryKey: ['newsletter-queue'] })
      // Approving/skipping removes the edition from the queue — move selection off it.
      if (action !== 'save') { setSelectedId(null); setEdits(null) }
      setMsg({ ok: true, text: action === 'skip' ? 'Skipped.' : action === 'approve' ? 'Approved.' : 'Saved.' })
      setTimeout(() => setMsg(null), 3000)
    },
    onError: () => {
      setMsg({ ok: false, text: 'Could not save — try again.' })
      setTimeout(() => setMsg(null), 5000)
    },
  })

  const regenerateMutation = useMutation({
    mutationFn: () =>
      api.post('/user/newsletter-draft/regenerate', {
        session_token: sessionToken,
        edition_id: draftEdit!.id,
        guidance: guidance.trim() || null,
      }),
    onSuccess: () => {
      const id = draftEdit!.id
      setGuidance('')
      setReseedId(id)
      setMsg({ ok: true, text: 'Regenerating… this can take a minute.' })
      // The task is async — refetch a couple of times to pick up the rewritten draft, then drop the
      // local edits so the editor shows the rewritten content rather than the text it replaced.
      setTimeout(() => qc.invalidateQueries({ queryKey: ['newsletter-queue'] }), 6000)
      setTimeout(() => {
        qc.invalidateQueries({ queryKey: ['newsletter-queue'] })
        setEdits((p) => (p?.id === id ? null : p))
        setReseedId(null)
        setMsg({ ok: true, text: 'Regenerated draft updated below.' })
        setTimeout(() => setMsg(null), 3000)
      }, 15000)
    },
    onError: () => {
      setMsg({ ok: false, text: 'Could not start regeneration — try again.' })
      setTimeout(() => setMsg(null), 5000)
    },
  })

  type CoverDetail = Pick<NewsletterEdition, 'cover_image_url' | 'cover_image_source' | 'cover_image_status'>
  const applyCover = (d: CoverDetail) =>
    setDe({
      cover_image_url: d.cover_image_url ?? null,
      cover_image_source: d.cover_image_source ?? null,
      cover_image_status: d.cover_image_status ?? null,
    })
  const coverError = (e: unknown, fallback: string) => {
    const detail = (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail
    setMsg({ ok: false, text: typeof detail === 'string' ? detail : fallback })
    setTimeout(() => setMsg(null), 6000)
  }

  const uploadCoverMutation = useMutation({
    mutationFn: (file: File) => {
      const form = new FormData()
      form.append('session_token', sessionToken!)
      form.append('edition_id', String(draftEdit!.id))
      form.append('file', file)
      return api.post('/user/newsletter-draft/cover', form)
    },
    onSuccess: (res) => {
      applyCover(res.data.detail as CoverDetail)
      qc.invalidateQueries({ queryKey: ['newsletter-queue'] })
      setMsg({ ok: true, text: 'Cover uploaded.' })
      setTimeout(() => setMsg(null), 3000)
    },
    onError: (e) => coverError(e, 'Could not upload that image — try another file.'),
  })

  // Per-edition avatar choice for AI covers: Auto lets the guardrails + relevance classifier
  // decide; 'with'/'without' override just this generation (never the master avatar switch).
  const [coverAvatarChoice, setCoverAvatarChoice] = useState<'auto' | 'with' | 'without'>('auto')

  const generateCoverMutation = useMutation({
    mutationFn: () =>
      api.post('/user/newsletter-draft/cover/generate', {
        session_token: sessionToken,
        edition_id: draftEdit!.id,
        use_avatar: coverAvatarChoice === 'auto' ? null : coverAvatarChoice === 'with',
      }),
    onSuccess: () => {
      setCoverWaitId(draftEdit!.id)
      // What the edition showed BEFORE this run: re-generating over an existing cover is the
      // normal case, so "has a cover" would be true on the very first poll and we would call the
      // OLD image ready while the new one is still rendering.
      setCoverWaitFrom(draftEdit!.cover_image_url ?? null)
      setMsg({ ok: true, text: 'Generating a cover… this can take a minute.' })
    },
    onError: (e) => coverError(e, 'Could not start cover generation — try again.'),
  })

  const coverDecisionMutation = useMutation({
    mutationFn: (action: 'approve' | 'remove') =>
      api.post('/user/newsletter-draft/cover/decision', {
        session_token: sessionToken,
        edition_id: draftEdit!.id,
        action,
      }),
    onSuccess: (res, action) => {
      applyCover(res.data.detail as CoverDetail)
      qc.invalidateQueries({ queryKey: ['newsletter-queue'] })
      setMsg({ ok: true, text: action === 'remove' ? 'Cover removed.' : 'Cover approved.' })
      setTimeout(() => setMsg(null), 3000)
    },
    onError: (e) => coverError(e, 'Could not update the cover — try again.'),
  })

  // Cover generation is an async task, so poll the queue until the edition carries a NEW cover —
  // one whose URL differs from whatever it had when generation started. The poll is BOUNDED: a
  // generation that failed (or was rejected by the cover gate) never lands a cover, and an
  // unbounded poll would leave the whole panel disabled forever.
  const COVER_POLL_LIMIT = 12
  useEffect(() => {
    if (coverWaitId == null) return
    let cancelled = false
    let timer: ReturnType<typeof setTimeout>
    let tries = 0

    const stop = () => {
      setCoverWaitId(null)
      setCoverWaitFrom(null)
    }
    const step = async () => {
      if (cancelled) return
      const res = await refetch()
      if (cancelled) return
      const fresh = res.data?.editions.find((e) => e.id === coverWaitId)
      if (fresh?.cover_image_url && fresh.cover_image_url !== coverWaitFrom) {
        stop()
        setMsg({ ok: true, text: 'Cover ready — review it below and approve to publish it.' })
        setTimeout(() => setMsg(null), 5000)
        return
      }
      if (++tries >= COVER_POLL_LIMIT) {
        stop()
        setMsg({ ok: false, text: 'No cover came back — try generating again or upload your own.' })
        setTimeout(() => setMsg(null), 8000)
        return
      }
      timer = setTimeout(step, 10000)
    }

    timer = setTimeout(step, 10000)
    return () => { cancelled = true; clearTimeout(timer) }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [coverWaitId, coverWaitFrom])

  const coverBusy =
    uploadCoverMutation.isPending || generateCoverMutation.isPending ||
    coverDecisionMutation.isPending || coverWaitId != null
  const regenerating = regenerateMutation.isPending || reseedId != null
  const busy = draftMutation.isPending || regenerating

  function selectEdition(e: NewsletterEdition) {
    setSelectedId(e.id)
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
            {(e.format || e.hook_style) && (
              <div className="flex flex-wrap gap-1 mt-1.5">
                {e.format && (
                  <span className="px-1.5 py-0.5 rounded bg-indigo-50 text-indigo-600 text-[10px] font-medium uppercase tracking-wide">
                    {prettyKey(e.format)}
                  </span>
                )}
                {e.hook_style && (
                  <span className="px-1.5 py-0.5 rounded bg-purple-50 text-purple-600 text-[10px] font-medium uppercase tracking-wide">
                    {prettyKey(e.hook_style)} hook
                  </span>
                )}
              </div>
            )}
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
                {...maskProps('w-full border border-gray-300 rounded-lg px-3 py-2 text-sm font-mono focus:outline-none focus:ring-2 focus:ring-blue-500')} />
            </div>
            <div>
              <label className="block text-sm font-medium text-gray-700 mb-1">
                Publish date &amp; time{' '}
                <span className="font-normal text-gray-400">
                  ({timezoneResolved ? userTimezone : 'loading your timezone…'})
                </span>
              </label>
              {/* Locked until the user's own zone has loaded — converting the typed wall clock
                  against the browser's guess would store an instant hours off (issue #774). */}
              <input
                type="datetime-local"
                value={timezoneResolved ? toZonedInputValue(draftEdit.scheduled_for, userTimezone) : ''}
                disabled={!timezoneResolved}
                onChange={(e) =>
                  setDe({ scheduled_for: zonedInputToUtcIso(e.target.value, userTimezone) ?? draftEdit.scheduled_for })
                }
                className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500 disabled:bg-gray-100"
              />
            </div>

            {/* Cover image (issue #893): upload your own, or generate one. A generated cover is
                NOT published until it's approved here — it's a public brand asset. */}
            <div className="border-t border-gray-100 pt-3 space-y-2">
              <div className="flex items-center justify-between">
                <label className="block text-sm font-medium text-gray-700">Cover image</label>
                {draftEdit.cover_image_url && (
                  <span
                    className={`px-2 py-0.5 rounded-full text-xs font-medium ${
                      draftEdit.cover_image_status === 'approved'
                        ? 'bg-green-100 text-green-700'
                        : 'bg-yellow-100 text-yellow-700'
                    }`}
                  >
                    {draftEdit.cover_image_status === 'approved' ? 'PUBLISHES WITH THIS EDITION' : 'NEEDS YOUR APPROVAL'}
                  </span>
                )}
              </div>
              {draftEdit.cover_image_url ? (
                <img src={draftEdit.cover_image_url} alt="Newsletter cover"
                  className="w-full rounded-lg border border-gray-200 object-cover" />
              ) : (
                <p className="text-xs text-gray-400">
                  No cover yet. Editions publish fine without one — a cover just makes the article
                  stand out in the feed and in subscriber email.
                </p>
              )}
              <input ref={coverFileRef} type="file" accept="image/png,image/jpeg,image/webp"
                className="hidden"
                onChange={(e) => {
                  const file = e.target.files?.[0]
                  if (file) uploadCoverMutation.mutate(file)
                  e.target.value = ''  // let the same file be re-picked after a rejection
                }} />
              <div className="grid grid-cols-2 gap-2">
                <button type="button" onClick={() => coverFileRef.current?.click()} disabled={busy || coverBusy}
                  className="bg-gray-100 text-gray-700 py-2 rounded-lg text-sm font-semibold hover:bg-gray-200 disabled:opacity-50 transition-colors">
                  {uploadCoverMutation.isPending ? 'Uploading…' : 'Upload image'}
                </button>
                <button type="button" onClick={() => generateCoverMutation.mutate()} disabled={busy || coverBusy}
                  className="bg-indigo-50 text-indigo-700 py-2 rounded-lg text-sm font-semibold hover:bg-indigo-100 disabled:opacity-50 transition-colors">
                  {coverWaitId != null ? 'Generating…' : 'Generate with AI'}
                </button>
              </div>
              <div className="flex items-center gap-2">
                <label htmlFor="cover-avatar-choice" className="text-xs text-gray-500">
                  My avatar in AI covers
                </label>
                <select id="cover-avatar-choice" value={coverAvatarChoice}
                  onChange={(e) => setCoverAvatarChoice(e.target.value as 'auto' | 'with' | 'without')}
                  disabled={busy || coverBusy}
                  className="text-xs border border-gray-200 rounded-md px-2 py-1 text-gray-700 bg-white">
                  <option value="auto">Auto — when the edition is about me</option>
                  <option value="with">Include me</option>
                  <option value="without">Never in this cover</option>
                </select>
              </div>
              {draftEdit.cover_image_url && (
                <div className="grid grid-cols-2 gap-2">
                  {draftEdit.cover_image_status !== 'approved' && (
                    <button type="button" onClick={() => coverDecisionMutation.mutate('approve')}
                      disabled={busy || coverBusy}
                      className="bg-green-600 text-white py-2 rounded-lg text-sm font-semibold hover:bg-green-700 disabled:opacity-50 transition-colors">
                      Approve cover
                    </button>
                  )}
                  <button type="button" onClick={() => coverDecisionMutation.mutate('remove')}
                    disabled={busy || coverBusy}
                    className={`text-gray-600 py-2 rounded-lg text-sm font-semibold bg-gray-100 hover:bg-gray-200 disabled:opacity-50 transition-colors ${
                      draftEdit.cover_image_status === 'approved' ? 'col-span-2' : ''
                    }`}>
                    Remove cover
                  </button>
                </div>
              )}
            </div>

            {/* Re-generate: the prominent action. Optional guidance steers the rewrite; empty guidance
                lets the AI pick a fresh, distinct take. */}
            <div className="border-t border-gray-100 pt-3 space-y-2">
              <label className="block text-sm font-medium text-gray-700 mb-1">
                Added Guidance <span className="font-normal text-gray-400">(optional)</span>
              </label>
              <textarea value={guidance} onChange={(e) => setGuidance(e.target.value)} rows={2}
                placeholder="What should change and why? You can name a format (e.g. case study, listicle, contrarian). Leave blank for a fresh, distinct take."
                disabled={busy}
                className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-indigo-500 disabled:opacity-50" />
              <button type="button" onClick={() => regenerateMutation.mutate()} disabled={busy}
                className="w-full bg-indigo-600 text-white py-2 rounded-lg text-sm font-semibold hover:bg-indigo-700 disabled:opacity-50 transition-colors">
                {regenerating ? 'Regenerating…' : 'Re-generate'}
              </button>
            </div>

            {msg && <p className={`text-sm font-medium ${msg.ok ? 'text-green-600' : 'text-red-600'}`}>{msg.text}</p>}
            <div className="grid grid-cols-2 gap-2">
              <button type="button" onClick={() => draftMutation.mutate('save')} disabled={busy}
                className="bg-gray-100 text-gray-700 py-2 rounded-lg text-sm font-semibold hover:bg-gray-200 disabled:opacity-50 transition-colors">
                Save Draft
              </button>
              <button type="button" onClick={() => draftMutation.mutate('approve')} disabled={busy}
                className="bg-blue-600 text-white py-2 rounded-lg text-sm font-semibold hover:bg-blue-700 disabled:opacity-50 transition-colors">
                Approve &amp; Schedule
              </button>
            </div>
            {/* Skip stays as a smaller secondary action — not publishing an edition is still useful. */}
            <button type="button" onClick={() => draftMutation.mutate('skip')} disabled={busy}
              className="w-full text-xs text-gray-400 hover:text-gray-600 disabled:opacity-50 transition-colors">
              Skip this edition
            </button>
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
