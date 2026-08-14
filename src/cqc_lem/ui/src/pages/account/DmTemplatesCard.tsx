import { useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import api from '../../api/client'
import { useAuth } from '../../contexts/useAuth'
import { DM_EVENTS } from './types'
import type { DmTemplate } from './types'
import { useRegisterSaveSection, sectionSaveCallbacks } from './settingsSave'
import PlaceholderChips from './PlaceholderChips'
import { FIELD_LIMITS } from './fieldLimits'
import { EVENTS, capture, maskProps } from '../../utils/analytics'

export default function DmTemplatesCard() {
  const { sessionToken } = useAuth()
  const queryClient = useQueryClient()
  const [dmTemplates, setDmTemplates] = useState<DmTemplate[]>([])
  const [dmInit, setDmInit] = useState(false)
  const [savedSig, setSavedSig] = useState<string | null>(null)
  const [dmMsg, setDmMsg] = useState<{ ok: boolean; text: string } | null>(null)

  const { data: dmData } = useQuery({
    queryKey: ['dm-templates', sessionToken],
    queryFn: () =>
      api
        .get(`/user/dm-templates?session_token=${encodeURIComponent(sessionToken!)}`)
        .then((r) => r.data.detail as DmTemplate[]),
    enabled: !!sessionToken,
    staleTime: 60 * 1000,
  })
  useEffect(() => {
    if (dmData && !dmInit) {
      const seeded: DmTemplate[] = [...dmData]
      for (const ev of DM_EVENTS) {
        if (!seeded.some((t) => t.event_type === ev.key && t.step === 0)) {
          seeded.push({ event_type: ev.key, step: 0, delay_hours: 0, template_text: '', is_active: true })
        }
      }
      setDmTemplates(seeded)
      setSavedSig(JSON.stringify(seeded))
      setDmInit(true)
    }
  }, [dmData, dmInit])

  const updateTemplate = (event_type: string, step: number, patch: Partial<DmTemplate>) =>
    setDmTemplates((ts) => ts.map((t) => (t.event_type === event_type && t.step === step ? { ...t, ...patch } : t)))
  const addFollowupStep = (event_type: string) =>
    setDmTemplates((ts) => {
      const nextStep = ts.filter((t) => t.event_type === event_type).reduce((m, t) => Math.max(m, t.step), -1) + 1
      return [...ts, { event_type, step: nextStep, delay_hours: 24, template_text: '', is_active: true }]
    })
  const removeStep = (event_type: string, step: number) =>
    setDmTemplates((ts) => ts.filter((t) => !(t.event_type === event_type && t.step === step)))

  const dmMutation = useMutation({
    mutationFn: () =>
      api.put('/user/dm-templates', {
        session_token: sessionToken,
        templates: dmTemplates.filter((t) => t.template_text.trim()),
      }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['dm-templates'] })
      setSavedSig(JSON.stringify(dmTemplates))
      capture(EVENTS.dmTemplateSaved, {
        templates: dmTemplates.filter((t) => t.template_text.trim()).length,
        // How many events have a follow-up sequence configured, not the message bodies.
        followup_steps: dmTemplates.filter((t) => t.step > 0 && t.template_text.trim()).length,
      })
      setDmMsg({ ok: true, text: 'DM templates saved.' })
      setTimeout(() => setDmMsg(null), 3000)
    },
    onError: () => {
      setDmMsg({ ok: false, text: 'Could not save — try again.' })
      setTimeout(() => setDmMsg(null), 5000)
    },
  })

  const isDirty = dmInit && savedSig !== null && JSON.stringify(dmTemplates) !== savedSig
  useRegisterSaveSection('dm-templates', 'DM Templates', isDirty,
    async () => { await dmMutation.mutateAsync(); return true })

  if (!dmInit) return null

  return (
    <div className="bg-white rounded-lg shadow-sm border border-gray-200 p-6 space-y-4">
      <h2 className="text-base font-semibold text-gray-700">DM Templates</h2>
      <p className="text-xs text-gray-500">
        Blank uses the built-in default. Add follow-up steps to message again after a delay — the
        sequence stops automatically if they reply. Click a placeholder to insert it at your cursor:
      </p>
      <PlaceholderChips placeholders={[
        { token: '{first_name}', desc: "The recipient's first name" },
        { token: '{headline}', desc: "The recipient's headline / role" },
        { token: '{blog_url}', desc: 'Your configured blog URL' },
        { token: '{event_detail}', desc: 'The Catch-up milestone (e.g. "started a new position at Acme")' },
      ]} />
      {DM_EVENTS.map((ev) => {
        const steps = dmTemplates.filter((t) => t.event_type === ev.key).sort((a, b) => a.step - b.step)
        return (
          <div key={ev.key} className="border-t border-gray-100 pt-4">
            <p className="text-sm font-semibold text-gray-700">{ev.label}</p>
            {steps.map((t) => (
              <div key={t.step} className="mt-2 space-y-1">
                <div className="flex items-center gap-2 text-xs text-gray-500">
                  <span>{t.step === 0 ? 'Initial message' : `Follow-up ${t.step}`}</span>
                  {t.step > 0 && (
                    <label className="flex items-center">
                      after
                      <input type="number" min={1} value={t.delay_hours}
                        onChange={(e) => updateTemplate(ev.key, t.step, { delay_hours: Number(e.target.value) })}
                        className="mx-1 w-16 border border-gray-300 rounded px-1 py-0.5" />
                      h
                    </label>
                  )}
                  {t.step > 0 && (
                    <button type="button" onClick={() => removeStep(ev.key, t.step)}
                      className="ml-auto text-red-500 hover:text-red-600">Remove</button>
                  )}
                </div>
                <textarea value={t.template_text} rows={2} maxLength={FIELD_LIMITS.dm_template}
                  onChange={(e) => updateTemplate(ev.key, t.step, { template_text: e.target.value })}
                  placeholder="Leave blank for the default message"
                  {...maskProps('w-full border border-gray-300 rounded-lg px-3 py-2 text-sm')} />
              </div>
            ))}
            <button type="button" onClick={() => addFollowupStep(ev.key)}
              className="mt-2 text-xs text-blue-600 font-medium hover:text-blue-700">+ Add follow-up</button>
          </div>
        )
      })}
      {dmMsg && (
        <p className={`text-sm font-medium ${dmMsg.ok ? 'text-green-600' : 'text-red-600'}`}>{dmMsg.text}</p>
      )}
      <button type="button" onClick={() => dmMutation.mutate(undefined, sectionSaveCallbacks('dm-templates'))}
        disabled={dmMutation.isPending}
        className="w-full bg-blue-600 text-white py-2 rounded-lg text-sm font-semibold hover:bg-blue-700 disabled:opacity-50 transition-colors">
        {dmMutation.isPending ? 'Saving…' : 'Save DM Templates'}
      </button>
    </div>
  )
}
