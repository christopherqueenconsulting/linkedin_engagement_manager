import { useState } from 'react'
import type { KeyboardEvent } from 'react'
import { isAxiosError } from 'axios'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import api from '../../api/client'
import { useAuth } from '../../contexts/useAuth'
import { maskProps } from '../../utils/analytics'

export interface CallCandidate {
  id: number
  ingest_id: number
  kind: string
  title: string
  body: string
  source_quote: string
  confidence: number
}

interface CallRow {
  id: number
  title: string | null
  status: string
  pending_count: number
}

export const BULK_CONFIDENCE = 0.8
const SOURCE_TYPES = [
  { key: 'otter', label: 'Otter.ai transcript (TXT, SRT, VTT)' },
  { key: 'wispr', label: 'Wispr Flow voice-note text' },
]

const errorText = (err: unknown): string => {
  if (isAxiosError(err) && err.response?.status === 404) {
    return 'Call ingest is not enabled for this account.'
  }
  return 'That did not work — try again.'
}

/**
 * Call and voice-note intake (issue #2629, Phase 1c). Owner-only until counsel signs off on the
 * privacy text: the caller renders it for admins, and the server keeps intake behind
 * `CALL_INGEST_ENABLED`. A source is registered only with the consent attestation, so there is no
 * way to upload without one. Candidates stay PENDING until reviewed here; only accepted or edited
 * ones reach the story bank.
 */
export default function CallIngestPanel() {
  const { sessionToken } = useAuth()
  const queryClient = useQueryClient()
  const tokenQs = `session_token=${encodeURIComponent(sessionToken!)}`

  const [sourceType, setSourceType] = useState('otter')
  const [consent, setConsent] = useState(false)
  const [file, setFile] = useState<File | null>(null)
  const [speakers, setSpeakers] = useState<string[] | null>(null)
  const [sourceIds, setSourceIds] = useState<Record<string, number>>({})
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null)
  const [focusId, setFocusId] = useState<number | null>(null)
  const [editing, setEditing] = useState<{ id: number; body: string } | null>(null)

  const { data: candidates = [] } = useQuery({
    queryKey: ['call-candidates', sessionToken],
    queryFn: () =>
      api.get(`/user/call-ingest/candidates?${tokenQs}`)
        .then((r) => (r.data.detail.candidates ?? []) as CallCandidate[]),
    enabled: !!sessionToken,
  })
  const { data: calls = [] } = useQuery({
    queryKey: ['call-calls', sessionToken],
    queryFn: () =>
      api.get(`/user/call-ingest/calls?${tokenQs}`)
        .then((r) => (r.data.detail.calls ?? []) as CallRow[]),
    enabled: !!sessionToken,
  })

  const refresh = () => Promise.all([
    queryClient.invalidateQueries({ queryKey: ['call-candidates'] }),
    queryClient.invalidateQueries({ queryKey: ['call-calls'] }),
    queryClient.invalidateQueries({ queryKey: ['story-bank'] }),
  ])
  const flash = (ok: boolean, text: string) => {
    setMsg({ ok, text })
    setTimeout(() => setMsg(null), 5000)
  }

  const uploadMutation = useMutation({
    mutationFn: async (ownSpeaker: string | null) => {
      let sourceId = sourceIds[sourceType]
      if (!sourceId) {
        const made = await api.post('/user/call-ingest/source', {
          session_token: sessionToken, source_type: sourceType, consent_attested: true,
        })
        sourceId = made.data.detail.source_id as number
        setSourceIds((ids) => ({ ...ids, [sourceType]: sourceId }))
      }
      const form = new FormData()
      form.append('session_token', sessionToken!)
      form.append('source_id', String(sourceId))
      form.append('source_type', sourceType)
      if (ownSpeaker) form.append('own_speaker', ownSpeaker)
      form.append('file', file!)
      return api.post('/user/call-ingest/upload', form)
    },
    onSuccess: async () => {
      setSpeakers(null)
      setFile(null)
      flash(true, 'Transcript received. Candidates appear here once extraction finishes.')
      await refresh()
    },
    onError: (err) => {
      // 409 is the "Is this you?" question, never a failure: the server will not guess whose
      // turns are the user's.
      if (isAxiosError(err) && err.response?.status === 409) {
        setSpeakers((err.response.data?.detail?.speakers ?? []) as string[])
        return
      }
      flash(false, errorText(err))
    },
  })

  const reviewMutation = useMutation({
    mutationFn: (v: { candidate_id: number; decision: 'accept' | 'edit' | 'reject'; edited_body?: string }) =>
      api.post('/user/call-ingest/review', { session_token: sessionToken, ...v }),
    onSuccess: async () => { setEditing(null); await refresh() },
    onError: (err) => flash(false, errorText(err)),
  })
  const bulkMutation = useMutation({
    mutationFn: () =>
      api.post('/user/call-ingest/accept-bulk', {
        session_token: sessionToken, min_confidence: BULK_CONFIDENCE,
      }),
    onSuccess: async (r) => {
      flash(true, `Accepted ${r.data.detail.accepted ?? 0} candidates.`)
      await refresh()
    },
    onError: (err) => flash(false, errorText(err)),
  })
  const purgeMutation = useMutation({
    mutationFn: () => api.delete('/user/call-ingest', { data: { session_token: sessionToken } }),
    onSuccess: async () => {
      setSourceIds({})
      flash(true, 'All call data deleted.')
      await refresh()
    },
    onError: (err) => flash(false, errorText(err)),
  })

  const purgeAll = () => {
    if (window.confirm('Delete every call transcript, candidate, source and story-bank entry '
      + 'learned from calls? This cannot be undone.')) {
      purgeMutation.mutate()
    }
  }

  const onKeyDown = (ev: KeyboardEvent<HTMLDivElement>) => {
    const tag = (ev.target as HTMLElement).tagName
    if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT' || ev.metaKey || ev.ctrlKey) return
    if (!candidates.length) return
    const at = Math.max(candidates.findIndex((c) => c.id === focusId), 0)
    const current = candidates[at]
    if (ev.key === 'j') setFocusId(candidates[Math.min(at + 1, candidates.length - 1)].id)
    else if (ev.key === 'k') setFocusId(candidates[Math.max(at - 1, 0)].id)
    else if (ev.key === 'a') reviewMutation.mutate({ candidate_id: current.id, decision: 'accept' })
    else if (ev.key === 'r') reviewMutation.mutate({ candidate_id: current.id, decision: 'reject' })
    else return
    ev.preventDefault()
  }

  // A Map keeps the server's order (newest call first). Integer-keyed object entries would
  // re-sort ascending and make j/k, which walks `candidates`, jump around the screen.
  const grouped = candidates.reduce<Map<number, CallCandidate[]>>((acc, c) => {
    const rows = acc.get(c.ingest_id) ?? []
    rows.push(c)
    return acc.set(c.ingest_id, rows)
  }, new Map())
  const callTitle = (id: number) => calls.find((c) => c.id === id)?.title || `Call #${id}`
  const activeId = focusId ?? candidates[0]?.id ?? null
  const above = candidates.filter((c) => c.confidence >= BULK_CONFIDENCE).length

  return (
    <div className="border border-hairline rounded-panel p-4 space-y-4" data-testid="call-ingest">
      <h3 className="text-sm font-semibold text-fg">Learn from calls and voice notes</h3>
      <p className="text-xs text-fg-muted">
        Upload a transcript and LEM proposes facts you actually said, each with the quote it came
        from. Only YOUR turns are kept, with other people's names, emails, phones and links removed
        first. Nothing reaches your posts until you accept it. The transcript is deleted once the
        call is fully reviewed, and always within 7 days.{' '}
        <a href="/privacy-policy" className="text-accent underline">Privacy policy (DRAFT)</a>
      </p>

      <div className="space-y-2">
        <select value={sourceType} aria-label="Source" onChange={(e) => setSourceType(e.target.value)}
          className="border border-field rounded-control px-2 py-1 text-sm">
          {SOURCE_TYPES.map((s) => <option key={s.key} value={s.key}>{s.label}</option>)}
        </select>
        <label className="flex items-start gap-2 text-xs text-fg">
          <input type="checkbox" checked={consent} aria-label="Consent attestation"
            onChange={(e) => setConsent(e.target.checked)} />
          Everyone on this call was told it was recorded.
        </label>
        <input type="file" accept=".vtt,.srt,.txt" aria-label="Transcript file"
          onChange={(e) => { setFile(e.target.files?.[0] ?? null); setSpeakers(null) }}
          className="block text-xs" />
        <button type="button" onClick={() => uploadMutation.mutate(null)}
          disabled={!consent || !file || uploadMutation.isPending}
          className="bg-accent text-white px-3 py-1 rounded-control text-sm font-semibold disabled:opacity-50">
          {uploadMutation.isPending ? 'Uploading…' : 'Upload transcript'}
        </button>
        {!consent && (
          <p className="text-xs text-fg-muted">Tick the attestation to enable upload.</p>
        )}
      </div>

      {speakers && (
        <div className="border border-hairline rounded-panel p-3 space-y-2" data-testid="own-speaker">
          <p className="text-sm font-medium text-fg">Is this you? Pick your name in this call.</p>
          <div className="flex flex-wrap gap-2">
            {speakers.map((s) => (
              <button key={s} type="button" onClick={() => uploadMutation.mutate(s)}
                className="border border-field rounded-control px-3 py-1 text-sm hover:bg-sunken">
                {s}
              </button>
            ))}
          </div>
        </div>
      )}

      {msg && <p className={`text-sm font-medium ${msg.ok ? 'text-success' : 'text-danger'}`}>{msg.text}</p>}

      {candidates.length > 0 && (
        <div className="space-y-3" tabIndex={0} onKeyDown={onKeyDown} data-testid="call-review"
             aria-label="Call candidates — j and k move, a accepts, r rejects">
          <div className="flex flex-wrap items-center gap-3">
            <p className="text-xs text-fg-muted">
              {candidates.length} to review · keys: j/k move, a accept, r reject
            </p>
            <button type="button" onClick={() => bulkMutation.mutate()}
              disabled={above === 0 || bulkMutation.isPending}
              className="text-xs text-accent font-medium disabled:opacity-50">
              Accept all above {BULK_CONFIDENCE} confidence ({above})
            </button>
          </div>
          {[...grouped.entries()].map(([ingestId, rows]) => (
            <div key={ingestId} className="space-y-2">
              <h4 className="text-xs font-semibold text-fg">{callTitle(ingestId)}</h4>
              {rows.map((c) => (
                <div key={c.id} data-testid={`candidate-${c.id}`}
                     className={`border rounded-panel p-3 space-y-2 ${
                       c.id === activeId ? 'border-accent' : 'border-hairline'}`}>
                  <p className="text-xs text-fg-muted">
                    {c.kind} · {Math.round(c.confidence * 100)}% · {c.title}
                  </p>
                  {editing?.id === c.id ? (
                    <textarea value={editing.body} rows={3} maxLength={5000} aria-label="Edit candidate"
                      onChange={(e) => setEditing({ id: c.id, body: e.target.value })}
                      {...maskProps('w-full border border-field rounded-panel px-3 py-2 text-sm')} />
                  ) : (
                    <p className="text-sm text-fg">{c.body}</p>
                  )}
                  <blockquote className="text-xs italic text-fg-muted border-l-2 border-hairline pl-2">
                    “{c.source_quote}”
                  </blockquote>
                  <div className="flex gap-3 text-xs font-medium">
                    {editing?.id === c.id ? (
                      <button type="button" disabled={!editing.body.trim()}
                        onClick={() => reviewMutation.mutate({
                          candidate_id: c.id, decision: 'edit', edited_body: editing.body })}
                        className="text-accent">Save edit</button>
                    ) : (
                      <>
                        <button type="button" className="text-accent"
                          onClick={() => reviewMutation.mutate({ candidate_id: c.id, decision: 'accept' })}>
                          Accept</button>
                        <button type="button" className="text-fg-muted"
                          onClick={() => setEditing({ id: c.id, body: c.body })}>Edit</button>
                        <button type="button" className="text-danger"
                          onClick={() => reviewMutation.mutate({ candidate_id: c.id, decision: 'reject' })}>
                          Reject</button>
                      </>
                    )}
                  </div>
                </div>
              ))}
            </div>
          ))}
        </div>
      )}

      <button type="button" onClick={purgeAll} disabled={purgeMutation.isPending}
        className="text-xs text-danger font-medium">
        Purge all call data
      </button>
    </div>
  )
}
