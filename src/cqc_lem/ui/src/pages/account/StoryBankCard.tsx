import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import api from '../../api/client'
import { useAuth } from '../../contexts/useAuth'
import { STORY_KINDS } from './types'
import type { GetDetail } from '../../api/types'
import type { StoryEntry, StoryKind } from './types'
import { useRegisterSaveSection, sectionSaveCallbacks } from './settingsSave'
import { EVENTS, capture, maskProps } from '../../utils/analytics'
import CallIngestPanel from './CallIngestPanel'

type StoryBankResponse = GetDetail<'/api/user/story-bank'>

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
  const { sessionToken, isAdmin } = useAuth()
  const queryClient = useQueryClient()
  // The edited list, once the user touches one — null means "show what the API returned". Derived
  // rather than seeded in an effect, so a refetch can never discard rows being typed.
  const [draft, setDraft] = useState<StoryEntry[] | null>(null)
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

  const entries: StoryEntry[] = draft ?? data?.entries ?? []
  const editEntries = (fn: (es: StoryEntry[]) => StoryEntry[]) =>
    setDraft((es) => fn(es ?? data?.entries ?? []))

  const update = (idx: number, patch: Partial<StoryEntry>) =>
    editEntries((es) => es.map((e, i) => (i === idx ? { ...e, ...patch } : e)))
  const addRow = () => editEntries((es) => [...es, blank()])

  const removeMutation = useMutation({
    mutationFn: (entry_id: number) =>
      api.delete('/user/story-bank', { data: { session_token: sessionToken, entry_id } }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['story-bank'] }),
  })
  const removeRow = (idx: number) => {
    const entry = entries[idx]
    editEntries((es) => es.filter((_, i) => i !== idx))
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
    onSuccess: async () => {
      // Wait for the refetch: React Query serves the pre-save rows until it lands, so dropping the
      // draft first would make a just-added entry disappear — and keep it gone if that refetch fails.
      await queryClient.invalidateQueries({ queryKey: ['story-bank'] })
      setDraft(null)
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

  const isDirty = !!data && JSON.stringify(entries) !== JSON.stringify(data.entries)
  useRegisterSaveSection('story-bank', 'Story Bank', isDirty,
    async () => { await saveMutation.mutateAsync(); return true })

  if (!data) return null

  const target = data?.target_entries ?? 5
  const filled = entries.filter((e) => e.body.trim() && e.active).length

  return (
    <div className="bg-raised rounded-panel shadow-panel border border-hairline p-6 space-y-4"
         data-testid="story-bank">
      <h2 className="text-base font-semibold text-fg">Story Bank</h2>
      <p className="text-xs text-fg-muted">
        Your voice tells LEM how you <span className="italic">sound</span>. This tells it what you've
        actually <span className="italic">done</span>. Each post is anchored to one entry, and no
        number, client or anecdote outside this bank is ever written about you — with an empty bank
        LEM writes industry observations instead of inventing a story.
      </p>
      <p className="text-xs text-fg-muted" data-testid="story-bank-progress">
        {filled} of {target} entries.{' '}
        {filled < target
          ? 'Aim for 5–10: a real number, a client win, a mistake, an opinion you hold.'
          : 'Nice — keep adding as things happen so posts never repeat the same story.'}
      </p>

      <div className="space-y-3">
        {entries.map((e, idx) => (
          <div key={e.id ?? `new-${idx}`}
               className="border border-hairline rounded-panel p-3 space-y-2">
            <div className="flex flex-wrap items-center gap-2">
              <select value={e.kind} aria-label="Kind"
                onChange={(ev) => update(idx, { kind: ev.target.value as StoryKind })}
                className="border border-field rounded-control px-2 py-1 text-sm">
                {STORY_KINDS.map((k) => (
                  <option key={k.key} value={k.key} title={k.hint}>{k.label}</option>
                ))}
              </select>
              <input type="text" value={e.title ?? ''} maxLength={255}
                onChange={(ev) => update(idx, { title: ev.target.value })}
                placeholder="Short label (optional)" aria-label="Title"
                className="flex-1 min-w-40 border border-field rounded-control px-3 py-1 text-sm" />
              <label className="flex items-center gap-1 text-xs text-fg-muted">
                When
                <input type="date" value={e.happened_at ?? ''} aria-label="When it happened"
                  onChange={(ev) => update(idx, { happened_at: ev.target.value || null })}
                  className="border border-field rounded-control px-2 py-1" />
              </label>
              <label className="flex items-center gap-1 text-xs text-fg-muted">
                <input type="checkbox" checked={e.active} aria-label="Active"
                  onChange={(ev) => update(idx, { active: ev.target.checked })} />
                On
              </label>
              <button type="button" onClick={() => removeRow(idx)}
                aria-label={`Remove ${e.title || 'entry'}`} title="Remove entry"
                className="text-danger hover:text-danger">×</button>
            </div>
            <textarea value={e.body} rows={3} maxLength={5000}
              onChange={(ev) => update(idx, { body: ev.target.value })}
              aria-label="What actually happened"
              placeholder="What actually happened — the real numbers, names, dates and outcome. Write it how you'd tell a colleague."
              {...maskProps('w-full border border-field rounded-panel px-3 py-2 text-sm')} />
            {typeof e.used_count === 'number' && e.used_count > 0 && (
              <p className="text-xs text-fg-muted">
                Used in {e.used_count} post{e.used_count === 1 ? '' : 's'} — LEM rotates to your
                least-used entries first.
              </p>
            )}
          </div>
        ))}
      </div>

      <button type="button" onClick={addRow}
        className="text-xs text-accent font-medium hover:text-accent">+ Add entry</button>

      {isAdmin && <CallIngestPanel />}

      {msg && <p className={`text-sm font-medium ${msg.ok ? 'text-success' : 'text-danger'}`}>{msg.text}</p>}
      <button type="button" onClick={() => saveMutation.mutate(undefined, sectionSaveCallbacks('story-bank'))}
        disabled={saveMutation.isPending}
        className="w-full bg-accent text-white py-2 rounded-panel text-sm font-semibold hover:bg-accent/90 disabled:opacity-50 transition-colors">
        {saveMutation.isPending ? 'Saving…' : 'Save Story Bank'}
      </button>
    </div>
  )
}
