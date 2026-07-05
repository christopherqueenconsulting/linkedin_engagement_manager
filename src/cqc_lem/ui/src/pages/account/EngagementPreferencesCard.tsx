import { useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import api from '../../api/client'
import { useAuth } from '../../contexts/AuthContext'
import Toggle from '../../components/Toggle'
import type { EngPrefs } from './types'

export default function EngagementPreferencesCard() {
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

  return (
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
      {engMsg && (
        <p className={`text-sm font-medium ${engMsg.ok ? 'text-green-600' : 'text-red-600'}`}>{engMsg.text}</p>
      )}
      <button type="button" onClick={() => engMutation.mutate()} disabled={engMutation.isPending}
        className="w-full bg-blue-600 text-white py-2 rounded-lg text-sm font-semibold hover:bg-blue-700 disabled:opacity-50 transition-colors">
        {engMutation.isPending ? 'Saving…' : 'Save Voice & Tone'}
      </button>
    </div>
  )
}
