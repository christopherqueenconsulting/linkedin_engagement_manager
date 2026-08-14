import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import api from '../../api/client'
import { useAuth } from '../../contexts/useAuth'
import Toggle from '../../components/Toggle'
import type { LeadMagnet } from './types'
import { useRegisterSaveSection, sectionSaveCallbacks } from './settingsSave'
import PlaceholderChips from './PlaceholderChips'
import { FIELD_LIMITS } from './fieldLimits'
import { maskProps } from '../../utils/analytics'

export default function LeadMagnetCard() {
  const { sessionToken } = useAuth()
  const queryClient = useQueryClient()
  // Only the fields the user has touched, laid over the API row at render time — so nothing is
  // seeded in an effect, and "dirty" is simply "the merged row differs from what the API returned".
  const [lmEdit, setLmEdit] = useState<Partial<LeadMagnet> | null>(null)
  const [lmMsg, setLmMsg] = useState<{ ok: boolean; text: string } | null>(null)

  const { data: lmData } = useQuery({
    queryKey: ['lead-magnet', sessionToken],
    queryFn: () =>
      api.get(`/user/lead-magnet?session_token=${encodeURIComponent(sessionToken!)}`).then((r) => r.data.detail as LeadMagnet),
    enabled: !!sessionToken,
    staleTime: 60 * 1000,
  })
  const leadMagnet: LeadMagnet | null = lmData ? { ...lmData, ...lmEdit } : null
  const setLm = (patch: Partial<LeadMagnet>) => setLmEdit((p) => ({ ...p, ...patch }))
  const lmMutation = useMutation({
    mutationFn: () => api.put('/user/lead-magnet', { session_token: sessionToken, ...leadMagnet }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['lead-magnet'] })
      setLmEdit(null)
      setLmMsg({ ok: true, text: 'Saved.' }); setTimeout(() => setLmMsg(null), 3000)
    },
    onError: () => { setLmMsg({ ok: false, text: 'Could not save — try again.' }); setTimeout(() => setLmMsg(null), 5000) },
  })

  const isDirty = !!lmData && JSON.stringify(leadMagnet) !== JSON.stringify(lmData)
  useRegisterSaveSection('lead-magnet', 'Lead Magnet', isDirty,
    async () => { await lmMutation.mutateAsync(); return true })

  if (!leadMagnet) return null

  return (
    <div className="bg-white rounded-lg shadow-sm border border-gray-200 p-6 space-y-4">
      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-base font-semibold text-gray-700">Comment → DM Lead Magnet</h2>
          <p className="text-xs text-gray-500">When someone comments your keyword on your post, auto-DM them a resource. The compliant way to share links (links in posts are penalized).</p>
        </div>
        <Toggle on={leadMagnet.enabled} onClick={() => setLm({ enabled: !leadMagnet.enabled })} />
      </div>
      {leadMagnet.enabled && (
        <>
          <div>
            <label className="block text-sm font-medium text-gray-700 mb-1">Trigger keyword</label>
            <input type="text" value={leadMagnet.keyword || ''} onChange={(e) => setLm({ keyword: e.target.value })}
              maxLength={FIELD_LIMITS.lm_keyword}
              placeholder="e.g. GUIDE" className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm" />
          </div>
          <div>
            <label className="block text-sm font-medium text-gray-700 mb-1">DM message (may include your link)</label>
            <textarea value={leadMagnet.message || ''} onChange={(e) => setLm({ message: e.target.value })} rows={3}
              maxLength={FIELD_LIMITS.lm_message}
              placeholder="Thanks for the interest! Here's the resource: …"
              {...maskProps('w-full border border-gray-300 rounded-lg px-3 py-2 text-sm')} />
            <div className="mt-1.5">
              <PlaceholderChips placeholders={[
                { token: '{first_name}', desc: "The commenter's first name" },
              ]} />
            </div>
          </div>
        </>
      )}
      {lmMsg && <p className={`text-sm font-medium ${lmMsg.ok ? 'text-green-600' : 'text-red-600'}`}>{lmMsg.text}</p>}
      <button type="button" onClick={() => lmMutation.mutate(undefined, sectionSaveCallbacks('lead-magnet'))}
        disabled={lmMutation.isPending}
        className="w-full bg-blue-600 text-white py-2 rounded-lg text-sm font-semibold hover:bg-blue-700 disabled:opacity-50 transition-colors">
        {lmMutation.isPending ? 'Saving…' : 'Save Lead Magnet'}
      </button>
    </div>
  )
}
