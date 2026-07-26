import { useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import api from '../../api/client'
import { useAuth } from '../../contexts/AuthContext'
import { TARGET_CATEGORIES } from './types'
import type { EngagementTarget, EngagementTargetCategory } from './types'
import { useRegisterSaveSection } from './SettingsSaveContext'

type RosterResponse = { targets: EngagementTarget[]; suggestions: EngagementTarget[] }

const blank = (): EngagementTarget => ({
  profile_url: '', name: '', category: 'peer', max_comments_per_week: 2, active: true, source: 'user',
})

// The blend the rotation aims for — shown live so the operator can see how far their roster is from
// the 50/30/20 mix the commenting task draws on.
function MixBar({ targets }: { targets: EngagementTarget[] }) {
  const active = targets.filter((t) => t.active && t.profile_url.trim())
  if (active.length === 0) return null
  return (
    <div className="flex flex-wrap gap-3 text-xs text-gray-600" data-testid="roster-mix">
      {TARGET_CATEGORIES.map((c) => {
        const n = active.filter((t) => t.category === c.key).length
        return (
          <span key={c.key}>
            {c.label}: <span className="font-semibold">{n}</span> (
            {Math.round((n / active.length) * 100)}%)
          </span>
        )
      })}
    </div>
  )
}

/**
 * Target-creator engagement roster (issue #616). LEM comments on these accounts' recent posts
 * BEFORE it looks at the home feed, at most `max_comments_per_week` times each so the rotation
 * never reads like a pod.
 */
export default function EngagementRosterCard() {
  const { sessionToken } = useAuth()
  const queryClient = useQueryClient()
  const [targets, setTargets] = useState<EngagementTarget[]>([])
  const [savedSig, setSavedSig] = useState<string | null>(null)
  const [init, setInit] = useState(false)
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null)

  const { data } = useQuery({
    queryKey: ['engagement-targets', sessionToken],
    queryFn: () =>
      api
        .get(`/user/engagement-targets?session_token=${encodeURIComponent(sessionToken!)}`)
        .then((r) => r.data.detail as RosterResponse),
    enabled: !!sessionToken,
    staleTime: 60 * 1000,
  })

  useEffect(() => {
    if (data && !init) {
      setTargets(data.targets)
      setSavedSig(JSON.stringify(data.targets))
      setInit(true)
    }
  }, [data, init])

  const update = (idx: number, patch: Partial<EngagementTarget>) =>
    setTargets((ts) => ts.map((t, i) => (i === idx ? { ...t, ...patch } : t)))
  const addRow = () => setTargets((ts) => [...ts, blank()])
  const addSuggestion = (s: EngagementTarget) =>
    setTargets((ts) => (ts.some((t) => t.profile_url === s.profile_url) ? ts : [...ts, { ...s }]))

  const removeMutation = useMutation({
    mutationFn: (profile_url: string) =>
      api.delete('/user/engagement-targets', { data: { session_token: sessionToken, profile_url } }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['engagement-targets'] }),
  })
  const removeRow = (idx: number) => {
    const target = targets[idx]
    setTargets((ts) => ts.filter((_, i) => i !== idx))
    // Only a row that was actually saved needs a server-side delete; a never-saved draft row just
    // disappears from local state.
    if (target?.id) removeMutation.mutate(target.profile_url)
  }

  const saveMutation = useMutation({
    mutationFn: () =>
      api.put('/user/engagement-targets', {
        session_token: sessionToken,
        targets: targets.filter((t) => t.profile_url.trim()),
      }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['engagement-targets'] })
      setSavedSig(JSON.stringify(targets))
      setMsg({ ok: true, text: 'Engagement roster saved.' })
      setTimeout(() => setMsg(null), 3000)
    },
    onError: () => {
      setMsg({ ok: false, text: 'Could not save — try again.' })
      setTimeout(() => setMsg(null), 5000)
    },
  })

  const isDirty = init && savedSig !== null && JSON.stringify(targets) !== savedSig
  useRegisterSaveSection('engagement-roster', 'Engagement Roster', isDirty,
    async () => { await saveMutation.mutateAsync(); return true })

  if (!init) return null

  const suggestions = (data?.suggestions ?? []).filter(
    (s) => !targets.some((t) => t.profile_url === s.profile_url))

  return (
    <div className="bg-white rounded-lg shadow-sm border border-gray-200 p-6 space-y-4"
         data-testid="engagement-roster">
      <h2 className="text-base font-semibold text-gray-700">Engagement Roster</h2>
      <p className="text-xs text-gray-500">
        LEM comments on these accounts' recent posts <span className="font-semibold">before</span> it
        looks at your home feed, at most the set number of times per week each. Aim for 50–100
        accounts, roughly 50% peers / 30% ICP / 20% large creators. Off-topic posts are always
        skipped, roster or not.
      </p>
      <MixBar targets={targets} />

      <div className="space-y-2">
        {targets.map((t, idx) => (
          <div key={t.id ?? `new-${idx}`} className="grid grid-cols-1 sm:grid-cols-12 gap-2 items-center">
            <input type="text" value={t.profile_url} maxLength={512}
              onChange={(e) => update(idx, { profile_url: e.target.value })}
              placeholder="https://www.linkedin.com/in/their-handle"
              aria-label="Profile URL"
              className="sm:col-span-5 border border-gray-300 rounded-lg px-3 py-2 text-sm" />
            <input type="text" value={t.name ?? ''} maxLength={255}
              onChange={(e) => update(idx, { name: e.target.value })}
              placeholder="Name (optional)" aria-label="Name"
              className="sm:col-span-3 border border-gray-300 rounded-lg px-3 py-2 text-sm" />
            <select value={t.category} aria-label="Category"
              onChange={(e) => update(idx, { category: e.target.value as EngagementTargetCategory })}
              className="sm:col-span-2 border border-gray-300 rounded-lg px-2 py-2 text-sm">
              {TARGET_CATEGORIES.map((c) => (
                <option key={c.key} value={c.key} title={c.hint}>{c.label}</option>
              ))}
            </select>
            <label className="sm:col-span-1 flex items-center gap-1 text-xs text-gray-500">
              <input type="number" min={0} max={14} value={t.max_comments_per_week}
                onChange={(e) => update(idx, { max_comments_per_week: Number(e.target.value) })}
                aria-label="Max comments per week"
                className="w-14 border border-gray-300 rounded px-1 py-1" />
              /wk
            </label>
            <div className="sm:col-span-1 flex items-center justify-end gap-2 text-xs">
              <label className="flex items-center gap-1 text-gray-500">
                <input type="checkbox" checked={t.active} aria-label="Active"
                  onChange={(e) => update(idx, { active: e.target.checked })} />
                On
              </label>
              <button type="button" onClick={() => removeRow(idx)}
                aria-label={`Remove ${t.name || t.profile_url || 'account'}`}
                title="Remove account"
                className="text-red-500 hover:text-red-600">×</button>
            </div>
          </div>
        ))}
      </div>

      <button type="button" onClick={addRow}
        className="text-xs text-blue-600 font-medium hover:text-blue-700">+ Add account</button>

      {suggestions.length > 0 && (
        <div className="border-t border-gray-100 pt-3 space-y-2">
          <p className="text-xs text-gray-500">
            Suggested seeds — people who recently engaged with your posts. Add them, then set the
            right category:
          </p>
          <div className="flex flex-wrap gap-2">
            {suggestions.map((s) => (
              <button key={s.profile_url} type="button" onClick={() => addSuggestion(s)}
                className="text-xs bg-gray-100 hover:bg-gray-200 text-gray-700 rounded-full px-3 py-1">
                + {s.name || s.profile_url}
              </button>
            ))}
          </div>
        </div>
      )}

      {msg && <p className={`text-sm font-medium ${msg.ok ? 'text-green-600' : 'text-red-600'}`}>{msg.text}</p>}
      <button type="button" onClick={() => saveMutation.mutate()} disabled={saveMutation.isPending}
        className="w-full bg-blue-600 text-white py-2 rounded-lg text-sm font-semibold hover:bg-blue-700 disabled:opacity-50 transition-colors">
        {saveMutation.isPending ? 'Saving…' : 'Save Roster'}
      </button>
    </div>
  )
}
