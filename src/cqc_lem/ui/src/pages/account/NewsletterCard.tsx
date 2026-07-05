import { useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import api from '../../api/client'
import { useAuth } from '../../contexts/AuthContext'
import Toggle from '../../components/Toggle'
import type { NewsletterSettings } from './types'

export default function NewsletterCard() {
  const { sessionToken } = useAuth()
  const queryClient = useQueryClient()
  const [newsletter, setNewsletter] = useState<NewsletterSettings | null>(null)
  const [nlMsg, setNlMsg] = useState<{ ok: boolean; text: string } | null>(null)

  const { data: nlData } = useQuery({
    queryKey: ['newsletter-settings', sessionToken],
    queryFn: () =>
      api
        .get(`/user/newsletter-settings?session_token=${encodeURIComponent(sessionToken!)}`)
        .then((r) => r.data.detail as NewsletterSettings),
    enabled: !!sessionToken,
    staleTime: 60 * 1000,
  })
  useEffect(() => {
    if (nlData && !newsletter) setNewsletter(nlData)
  }, [nlData])

  const setNl = (patch: Partial<NewsletterSettings>) => setNewsletter((p) => (p ? { ...p, ...patch } : p))

  const nlMutation = useMutation({
    mutationFn: () => api.put('/user/newsletter-settings', { session_token: sessionToken, ...newsletter }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['newsletter-settings'] })
      setNlMsg({ ok: true, text: 'Saved.' })
      setTimeout(() => setNlMsg(null), 3000)
    },
    onError: () => {
      setNlMsg({ ok: false, text: 'Could not save — try again.' })
      setTimeout(() => setNlMsg(null), 5000)
    },
  })

  if (!newsletter) return null

  return (
    <div className="bg-white rounded-lg shadow-sm border border-gray-200 p-6 space-y-4">
      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-base font-semibold text-gray-700">LinkedIn Newsletter</h2>
          <p className="text-xs text-gray-500">Auto-publish a recurring newsletter (bypasses the feed — subscribers get a notification + email). Repurposes your blog when set.</p>
        </div>
        <Toggle on={newsletter.enabled} onClick={() => setNl({ enabled: !newsletter.enabled })} />
      </div>
      {newsletter.enabled && (
        <>
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
            <div>
              <label className="block text-sm font-medium text-gray-700 mb-1">Title</label>
              <input type="text" value={newsletter.title || ''} onChange={(e) => setNl({ title: e.target.value })}
                placeholder="e.g. The Growth Brief" className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm" />
            </div>
            <div>
              <label className="block text-sm font-medium text-gray-700 mb-1">Cadence</label>
              <select value={newsletter.cadence} onChange={(e) => setNl({ cadence: e.target.value })}
                className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm">
                <option value="weekly">Weekly (recommended)</option>
                <option value="biweekly">Bi-weekly</option>
                <option value="monthly">Monthly</option>
              </select>
            </div>
          </div>
          <div>
            <label className="block text-sm font-medium text-gray-700 mb-1">Topic / theme</label>
            <input type="text" value={newsletter.topic || ''} onChange={(e) => setNl({ topic: e.target.value })}
              placeholder="What each edition should focus on" className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm" />
          </div>
          <div className="flex items-center justify-between">
            <p className="text-sm font-medium text-gray-700">Align with my blog</p>
            <Toggle on={newsletter.align_with_blog} onClick={() => setNl({ align_with_blog: !newsletter.align_with_blog })} />
          </div>
        </>
      )}
      {nlMsg && <p className={`text-sm font-medium ${nlMsg.ok ? 'text-green-600' : 'text-red-600'}`}>{nlMsg.text}</p>}
      <button type="button" onClick={() => nlMutation.mutate()} disabled={nlMutation.isPending}
        className="w-full bg-blue-600 text-white py-2 rounded-lg text-sm font-semibold hover:bg-blue-700 disabled:opacity-50 transition-colors">
        {nlMutation.isPending ? 'Saving…' : 'Save Newsletter Settings'}
      </button>
    </div>
  )
}
