import { useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import api from '../../api/client'
import { useAuth } from '../../contexts/useAuth'
import { STORY_KINDS } from './types'
import type { StoryEntry, StoryKind } from './types'
import { useRegisterSaveSection, sectionSaveCallbacks } from './settingsSave'
import { EVENTS, capture, maskProps } from '../../utils/analytics'

type StoryBankResponse = { entries: StoryEntry[]; kinds: StoryKind[]; target_entries: number }

const blank = (): StoryEntry => ({
  kind: 'anecdote', title: '', body: '', happened_at: null, active: true,
})

/**
 * Story bank / fact intake (issue #620). Your voice tells LEM how you sound; this tells it what you
 * have actually done. Every generated post is anchored to one entry, and nothing outside the bank
 * may be stated as a personal specific — so an empty bank means observation posts, never invented
 * anecdotes. Quick capture on purpose: one textarea per entry, everything else optional.
 */
export default function StoryBankCard() {
  const { sessionToken } = useAuth()
  const queryClient = useQueryClient()
  const [entries, setEntries] = useState<StoryEntry[]>([])
  const [savedSig, setSavedSig] = useState<string | null>(null)
  const [init, setInit] = useState(false)
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null)

  const { data } = useQuery({
    queryKey: ['story-bank', sessionToken],
    queryFn: () =>
      api
        .get(`/user/story-bank?session_token=${encodeURIComponent(sessionToken!)}`)
        .then((r) => r.data.detail as StoryBankResponse),
    enabled: !!sessionToken,
    staleTime: 60 * 1000,
  })

  useEffect(() => {
    if (data && !init) {
      setEntries(data.entries)
      setSavedSig(JSON.stringify(data.entries))
      setInit(true)
    }
  }, [data, init])

  const update = (idx: number, patch: Partial<StoryEntry>) =>
    setEntries((es) => es.map((e, i) => (i === idx ? { ...e, ...patch } : e)))
  const addRow = () => setEntries((es) => [...es, blank()])

  const removeMutation = useMutation({
    mutationFn: (entry_id: number) =>
      api.delete('/user/story-bank', { data: { session_token: sessionToken, entry_id } }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['story-bank'] }),
  })
  const removeRow = (idx: number) => {
    const entry = entries[idx]
    setEntries((es) => es.filter((_, i) => i !== idx))
    // Only a row that was actually saved needs a server-side delete; a never-saved draft row just
    // disappears from local state.
    if (entry?.id) removeMutation.mutate(entry.id)
  }

  const saveMutation = useMutation({
    mutationFn: () =>
      api.put('/user/story-bank', {
        session_token: sessionToken,
        entries: entries.filter((e) => e.body.trim()),
      }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['story-bank'] })
      setSavedSig(JSON.stringify(entries))
      // Fired on the SAVE, not on "+ Add entry" — a blank row the user abandons never became an
      // entry. Counts and kinds only; the entry text is the user's own material.
      const added = entries.filter((e) => !e.id && e.body.trim())
      if (added.length) {
        capture(EVENTS.storyBankEntryAdded, {
          added: added.length,
          total: entries.filter((e) => e.body.trim()).length,
          kinds: [...new Set(added.map((e) => e.kind))],
        })
      }
      setMsg({ ok: true, text: 'Story bank saved.' })
      setTimeout(() => setMsg(null), 3000)
    },
    onError: () => {
      setMsg({ ok: false, text: 'Could not save — try again.' })
      setTimeout(() => setMsg(null), 5000)
    },
  })

  const isDirty = init && savedSig !== null && JSON.stringify(entries) !== savedSig
  useRegisterSaveSection('story-bank', 'Story Bank', isDirty,
    async () => { await saveMutation.mutateAsync(); return true })

  if (!init) return null

  const target = data?.target_entries ?? 5
  const filled = entries.filter((e) => e.body.trim() && e.active).length

  return (
    <div className="bg-white rounded-lg shadow-sm border border-gray-200 p-6 space-y-4"
         data-testid="story-bank">
      <h2 className="text-base font-semibold text-gray-700">Story Bank</h2>
      <p className="text-xs text-gray-500">
        Your voice tells LEM how you <span className="italic">sound</span>. This tells it what you've
        actually <span className="italic">done</span>. Each post is anchored to one entry, and no
        number, client or anecdote outside this bank is ever written about you — with an empty bank
        LEM writes industry observations instead of inventing a story.
      </p>
      <p className="text-xs text-gray-500" data-testid="story-bank-progress">
        {filled} of {target} entries.{' '}
        {filled < target
          ? 'Aim for 5–10: a real number, a client win, a mistake, an opinion you hold.'
          : 'Nice — keep adding as things happen so posts never repeat the same story.'}
      </p>

      <div className="space-y-3">
        {entries.map((e, idx) => (
          <div key={e.id ?? `new-${idx}`}
               className="border border-gray-200 rounded-lg p-3 space-y-2">
            <div className="flex flex-wrap items-center gap-2">
              <select value={e.kind} aria-label="Kind"
                onChange={(ev) => update(idx, { kind: ev.target.value as StoryKind })}
                className="border border-gray-300 rounded-lg px-2 py-1 text-sm">
                {STORY_KINDS.map((k) => (
                  <option key={k.key} value={k.key} title={k.hint}>{k.label}</option>
                ))}
              </select>
              <input type="text" value={e.title ?? ''} maxLength={255}
                onChange={(ev) => update(idx, { title: ev.target.value })}
                placeholder="Short label (optional)" aria-label="Title"
                className="flex-1 min-w-40 border border-gray-300 rounded-lg px-3 py-1 text-sm" />
              <label className="flex items-center gap-1 text-xs text-gray-500">
                When
                <input type="date" value={e.happened_at ?? ''} aria-label="When it happened"
                  onChange={(ev) => update(idx, { happened_at: ev.target.value || null })}
                  className="border border-gray-300 rounded px-2 py-1" />
              </label>
              <label className="flex items-center gap-1 text-xs text-gray-500">
                <input type="checkbox" checked={e.active} aria-label="Active"
                  onChange={(ev) => update(idx, { active: ev.target.checked })} />
                On
              </label>
              <button type="button" onClick={() => removeRow(idx)}
                aria-label={`Remove ${e.title || 'entry'}`} title="Remove entry"
                className="text-red-500 hover:text-red-600">×</button>
            </div>
            <textarea value={e.body} rows={3} maxLength={5000}
              onChange={(ev) => update(idx, { body: ev.target.value })}
              aria-label="What actually happened"
              placeholder="What actually happened — the real numbers, names, dates and outcome. Write it how you'd tell a colleague."
              {...maskProps('w-full border border-gray-300 rounded-lg px-3 py-2 text-sm')} />
            {typeof e.used_count === 'number' && e.used_count > 0 && (
              <p className="text-[11px] text-gray-400">
                Used in {e.used_count} post{e.used_count === 1 ? '' : 's'} — LEM rotates to your
                least-used entries first.
              </p>
            )}
          </div>
        ))}
      </div>

      <button type="button" onClick={addRow}
        className="text-xs text-blue-600 font-medium hover:text-blue-700">+ Add entry</button>

      {msg && <p className={`text-sm font-medium ${msg.ok ? 'text-green-600' : 'text-red-600'}`}>{msg.text}</p>}
      <button type="button" onClick={() => saveMutation.mutate(undefined, sectionSaveCallbacks('story-bank'))}
        disabled={saveMutation.isPending}
        className="w-full bg-blue-600 text-white py-2 rounded-lg text-sm font-semibold hover:bg-blue-700 disabled:opacity-50 transition-colors">
        {saveMutation.isPending ? 'Saving…' : 'Save Story Bank'}
      </button>
    </div>
  )
}
