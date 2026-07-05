import { useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import api from '../../api/client'
import { useAuth } from '../../contexts/AuthContext'
import Toggle from '../../components/Toggle'
import { csv, parseCsv } from './types'
import type { EngPrefs } from './types'

// Voice & Tone and Engagement Targeting both edit the SAME engagement_preferences object and each
// PUT the full object. They must therefore share one piece of local state — otherwise saving one
// card would overwrite the other card's (stale) copy and revert its changes. So this single
// component owns the shared engPrefs state + mutation and renders both cards.
export default function EngagementSettingsCard() {
  const { sessionToken } = useAuth()
  const queryClient = useQueryClient()
  const [engPrefs, setEngPrefs] = useState<EngPrefs | null>(null)
  const [engMsg, setEngMsg] = useState<{ ok: boolean; text: string } | null>(null)

  const { data: engData } = useQuery({
    queryKey: ['engagement-preferences', sessionToken],
    queryFn: () =>
      api
        .get(`/user/engagement-preferences?session_token=${encodeURIComponent(sessionToken!)}`)
        .then((r) => r.data.detail as EngPrefs),
    enabled: !!sessionToken,
    staleTime: 60 * 1000,
  })
  useEffect(() => {
    if (engData && !engPrefs) setEngPrefs(engData)
  }, [engData])

  const setEng = (patch: Partial<EngPrefs>) => setEngPrefs((p) => (p ? { ...p, ...patch } : p))

  const engMutation = useMutation({
    mutationFn: () => api.put('/user/engagement-preferences', { session_token: sessionToken, ...engPrefs }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['engagement-preferences'] })
      setEngMsg({ ok: true, text: 'Saved.' })
      setTimeout(() => setEngMsg(null), 3000)
    },
    onError: () => {
      setEngMsg({ ok: false, text: 'Could not save — try again.' })
      setTimeout(() => setEngMsg(null), 5000)
    },
  })

  if (!engPrefs) return null

  const saveBtn = (label: string) => (
    <button type="button" onClick={() => engMutation.mutate()} disabled={engMutation.isPending}
      className="w-full bg-blue-600 text-white py-2 rounded-lg text-sm font-semibold hover:bg-blue-700 disabled:opacity-50 transition-colors">
      {engMutation.isPending ? 'Saving…' : label}
    </button>
  )

  return (
    <>
      {/* Voice & Tone */}
      <div className="bg-white rounded-lg shadow-sm border border-gray-200 p-6 space-y-5">
        <h2 className="text-base font-semibold text-gray-700">Voice &amp; Tone</h2>
        <p className="text-xs text-gray-500">
          How AI comments and DMs should sound. Leave blank to infer purely from your profile.
        </p>
        <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
          <div>
            <label className="block text-sm font-medium text-gray-700 mb-1">Tone</label>
            <input type="text" value={engPrefs.tone || ''} onChange={(e) => setEng({ tone: e.target.value })}
              placeholder="e.g. warm, authoritative"
              className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm" />
          </div>
          <div>
            <label className="block text-sm font-medium text-gray-700 mb-1">Comment length</label>
            <select value={engPrefs.comment_length} onChange={(e) => setEng({ comment_length: e.target.value })}
              className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm">
              <option value="short">Short (~300 chars)</option>
              <option value="medium">Medium (~600 chars)</option>
              <option value="long">Long (~1100 chars)</option>
            </select>
          </div>
        </div>
        <div>
          <label className="block text-sm font-medium text-gray-700 mb-1">Style guidance</label>
          <input type="text" value={engPrefs.comment_style || ''} onChange={(e) => setEng({ comment_style: e.target.value })}
            placeholder="e.g. ask a question, avoid buzzwords"
            className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm" />
        </div>
        <div className="flex items-center justify-between">
          <p className="text-sm font-medium text-gray-700">Use emojis</p>
          <Toggle on={engPrefs.use_emojis} onClick={() => setEng({ use_emojis: !engPrefs.use_emojis })} />
        </div>
        <div className="flex items-center justify-between">
          <p className="text-sm font-medium text-gray-700">Use hashtags</p>
          <Toggle on={engPrefs.use_hashtags} onClick={() => setEng({ use_hashtags: !engPrefs.use_hashtags })} />
        </div>
        {saveBtn('Save Voice & Tone')}
      </div>

      {/* Engagement Targeting */}
      <div className="bg-white rounded-lg shadow-sm border border-gray-200 p-6 space-y-5">
        <h2 className="text-base font-semibold text-gray-700">Engagement Targeting</h2>
        <p className="text-xs text-gray-500">
          Control which posts LEM comments on. Comma-separated. Exclusions always win; if any
          "include" is set, a post must match one (keyword/author literal, or a topic via AI relevance).
        </p>
        {([
          ['include_topics', 'Include topics (AI relevance)'],
          ['exclude_topics', 'Exclude topics'],
          ['include_keywords', 'Include keywords'],
          ['exclude_keywords', 'Exclude keywords'],
          ['include_authors', 'Include authors'],
          ['exclude_authors', 'Exclude authors'],
        ] as [keyof EngPrefs, string][]).map(([field, label]) => (
          <div key={field}>
            <label className="block text-sm font-medium text-gray-700 mb-1">{label}</label>
            <input type="text" value={csv(engPrefs[field] as string[])}
              onChange={(e) => setEng({ [field]: parseCsv(e.target.value) } as Partial<EngPrefs>)}
              placeholder="comma, separated, values"
              className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm" />
          </div>
        ))}
        <div className="grid grid-cols-1 sm:grid-cols-3 gap-4">
          <div>
            <label className="block text-sm font-medium text-gray-700 mb-1">Min. reactions</label>
            <input type="number" min={0} value={engPrefs.min_reactions ?? ''}
              onChange={(e) => setEng({ min_reactions: e.target.value === '' ? null : Number(e.target.value) })}
              className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm" />
          </div>
          <div>
            <label className="block text-sm font-medium text-gray-700 mb-1">Max post age (hrs)</label>
            <input type="number" min={1} value={engPrefs.max_post_age_hours ?? ''}
              onChange={(e) => setEng({ max_post_age_hours: e.target.value === '' ? null : Number(e.target.value) })}
              placeholder="24"
              className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm" />
          </div>
          <div>
            <label className="block text-sm font-medium text-gray-700 mb-1">Max comments/day</label>
            <input type="number" min={0} value={engPrefs.max_comments_per_day}
              onChange={(e) => setEng({ max_comments_per_day: Number(e.target.value) })}
              className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm" />
          </div>
          <div>
            <label className="block text-sm font-medium text-gray-700 mb-1">Max DMs/day</label>
            <input type="number" min={0} value={engPrefs.max_dms_per_day}
              onChange={(e) => setEng({ max_dms_per_day: Number(e.target.value) })}
              className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm" />
          </div>
        </div>
        <div className="flex items-center justify-between">
          <p className="text-sm font-medium text-gray-700">Reply to comments on my posts</p>
          <Toggle on={engPrefs.reply_to_own_comments} onClick={() => setEng({ reply_to_own_comments: !engPrefs.reply_to_own_comments })} />
        </div>
        {engMsg && (
          <p className={`text-sm font-medium ${engMsg.ok ? 'text-green-600' : 'text-red-600'}`}>{engMsg.text}</p>
        )}
        {saveBtn('Save Targeting')}
      </div>
    </>
  )
}
