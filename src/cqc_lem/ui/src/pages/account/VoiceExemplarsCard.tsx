import { useState } from 'react'
import { isAxiosError } from 'axios'
import { useMutation } from '@tanstack/react-query'
import api from '../../api/client'
import { useAuth } from '../../contexts/useAuth'
import SettingsCard from '../../components/SettingsCard'
import { summarize, type IngestResult } from './voiceExemplarsSummary'

// Mirrors `voice_exemplars.MAX_UPLOAD_BYTES`; the server is the authority, this only saves a round trip.
export const MAX_EXPORT_BYTES = 5 * 1024 * 1024

const KINDS = [
  { key: 'comments', label: 'Comments.csv', hint: 'Your comments on other people’s posts' },
  { key: 'shares', label: 'Shares.csv', hint: 'Posts you wrote yourself' },
] as const

const errorText = (err: unknown): string => {
  if (isAxiosError(err)) {
    if (err.response?.status === 404) return 'Writing-sample upload is not enabled for this account.'
    const detail = err.response?.data?.detail
    if (err.response?.status === 400 && typeof detail === 'string') return detail
  }
  return 'That upload did not work — try again.'
}

/**
 * Upload of the user's own LinkedIn data-export files (issue #2627, Phase 1b). Comments and posts
 * they wrote by hand become the examples a comment or DM is written "like". The server keeps the
 * feature behind `VOICE_EXEMPLARS`, so a 404 reads as "not enabled" rather than a failure.
 */
export default function VoiceExemplarsCard() {
  const { sessionToken } = useAuth()
  const [kind, setKind] = useState<(typeof KINDS)[number]['key']>('comments')
  const [file, setFile] = useState<File | null>(null)
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null)

  const upload = useMutation({
    mutationFn: () => {
      const form = new FormData()
      form.append('session_token', sessionToken!)
      form.append('kind', kind)
      form.append('file', file!)
      return api.post('/user/voice-exemplars', form).then((r) => r.data.detail as IngestResult)
    },
    onSuccess: (result) => {
      setFile(null)
      setMsg({ ok: !result.unavailable, text: summarize(result) })
    },
    onError: (err) => setMsg({ ok: false, text: errorText(err) }),
  })

  const pick = (picked: File | null) => {
    setMsg(null)
    if (picked && picked.size > MAX_EXPORT_BYTES) {
      setFile(null)
      setMsg({ ok: false, text: `That file is larger than ${MAX_EXPORT_BYTES / (1024 * 1024)} MB.` })
      return
    }
    setFile(picked)
  }

  return (
    <SettingsCard
      title="Your own writing"
      subtitle={'Upload Comments.csv or Shares.csv from your LinkedIn data export. LEM reads your own '
        + 'comments and posts so drafts sound like you, and never copies them.'}
    >
      <div className="grid grid-cols-1 sm:grid-cols-2 gap-2">
        {KINDS.map((k) => (
          <label key={k.key}
            className={`border rounded-control px-3 py-2 text-sm cursor-pointer ${
              kind === k.key ? 'border-accent text-fg' : 'border-field text-fg-muted'
            }`}>
            <input type="radio" name="voice-kind" value={k.key} checked={kind === k.key}
              onChange={() => setKind(k.key)} className="mr-2" />
            <span className="font-medium">{k.label}</span>
            <span className="block text-xs text-fg-muted">{k.hint}</span>
          </label>
        ))}
      </div>

      <input type="file" accept=".csv,text/csv" data-testid="voice-exemplars-file"
        onChange={(e) => pick(e.target.files?.[0] ?? null)}
        className="block w-full text-sm text-fg" />

      {msg && (
        <p role="status" className={`text-sm font-medium ${msg.ok ? 'text-success' : 'text-danger'}`}>
          {msg.text}
        </p>
      )}

      <button type="button" data-testid="voice-exemplars-upload"
        onClick={() => upload.mutate()}
        disabled={upload.isPending || !file}
        className={'w-full bg-accent text-white py-2 rounded-panel text-sm font-semibold '
          + 'hover:bg-accent/90 disabled:opacity-50 transition-colors'}>
        {upload.isPending ? 'Uploading…' : 'Upload'}
      </button>
    </SettingsCard>
  )
}
