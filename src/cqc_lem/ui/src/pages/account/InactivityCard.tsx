import { useEffect, useState } from 'react'
import { useMutation, useQuery } from '@tanstack/react-query'
import api from '../../api/client'
import { useAuth } from '../../contexts/AuthContext'

const INACTIVATE_OPTIONS = [
  { label: '30 days', value: 30 },
  { label: '60 days', value: 60 },
  { label: '90 days (default)', value: 90 },
  { label: '120 days', value: 120 },
  { label: '365 days', value: 365 },
  { label: 'Never', value: null },
]

// BCP-47 tags kept in lockstep with users.content_language VARCHAR(16). Empty = inherit the
// Login Location locale. Drives the language of premium (Veo) video audio — issue #548.
const CONTENT_LANGUAGE_OPTIONS = [
  { label: 'English (US)', value: 'en-US' },
  { label: 'English (UK)', value: 'en-GB' },
  { label: 'Spanish', value: 'es-ES' },
  { label: 'Spanish (Latin America)', value: 'es-419' },
  { label: 'French', value: 'fr-FR' },
  { label: 'German', value: 'de-DE' },
  { label: 'Portuguese (Brazil)', value: 'pt-BR' },
  { label: 'Italian', value: 'it-IT' },
  { label: 'Dutch', value: 'nl-NL' },
  { label: 'Japanese', value: 'ja-JP' },
  { label: 'Korean', value: 'ko-KR' },
  { label: 'Chinese (Simplified)', value: 'zh-CN' },
  { label: 'Hindi', value: 'hi-IN' },
  { label: 'Arabic', value: 'ar-SA' },
]

export default function InactivityCard() {
  const { sessionToken } = useAuth()
  const [inactivateDelay, setInactivateDelay] = useState<number | null>(90)
  const [autoSchedule, setAutoSchedule] = useState(false)
  const [contentLanguage, setContentLanguage] = useState('')
  const [prefsInitialised, setPrefsInitialised] = useState(false)
  const [prefsSavedMsg, setPrefsSavedMsg] = useState<string | null>(null)

  const { data: settingsData } = useQuery({
    queryKey: ['user-settings', sessionToken],
    queryFn: () =>
      api
        .get(`/user/settings?session_token=${encodeURIComponent(sessionToken!)}`)
        .then((r) => r.data.detail as {
          subscription: {
            status: string | null
            tier: string | null
            trial_started_at: string | null
            trial_ends_at: string | null
            stripe_customer_id: string | null
          } | null
          preferences: {
            last_login_inactivate_delay: number | null
            auto_schedule_posts: boolean
            content_language: string | null
            effective_content_language: string | null
          } | null
          blog_url: string | null
          sitemap_url: string | null
          company_linked_in_url: string | null
        }),
    enabled: !!sessionToken,
    staleTime: 60 * 1000,
  })

  useEffect(() => {
    if (settingsData?.preferences && !prefsInitialised) {
      setInactivateDelay(settingsData.preferences.last_login_inactivate_delay)
      setAutoSchedule(settingsData.preferences.auto_schedule_posts)
      setContentLanguage(settingsData.preferences.content_language ?? '')
      setPrefsInitialised(true)
    }
  }, [settingsData, prefsInitialised])

  const prefsMutation = useMutation({
    mutationFn: () =>
      api.put('/user/settings', {
        session_token: sessionToken,
        last_login_inactivate_delay: inactivateDelay,
        auto_schedule_posts: autoSchedule,
        content_language: contentLanguage,
      }),
    onSuccess: () => {
      setPrefsSavedMsg('Preferences saved!')
      setTimeout(() => setPrefsSavedMsg(null), 3000)
    },
    onError: () => {
      setPrefsSavedMsg('Save failed — please try again.')
      setTimeout(() => setPrefsSavedMsg(null), 5000)
    },
  })

  function handlePrefsSave(e: React.FormEvent) {
    e.preventDefault()
    prefsMutation.mutate()
  }

  return (
    <form onSubmit={handlePrefsSave} className="bg-white rounded-lg shadow-sm border border-gray-200 p-6 space-y-5">
      <h2 className="text-base font-semibold text-gray-700">Preferences</h2>

      <div>
        <label className="block text-sm font-medium text-gray-700 mb-1">
          Inactivity auto-stop delay
        </label>
        <select
          value={inactivateDelay === null ? 'never' : String(inactivateDelay)}
          onChange={(e) =>
            setInactivateDelay(e.target.value === 'never' ? null : Number(e.target.value))
          }
          className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
        >
          {INACTIVATE_OPTIONS.map(({ label, value }) => (
            <option key={String(value)} value={value === null ? 'never' : String(value)}>
              {label}
            </option>
          ))}
        </select>
        <p className="text-xs text-gray-400 mt-1">
          If you haven't logged in within this window, LEM will pause automated posting and LinkedIn activity to avoid acting on your behalf unintentionally.
          Set to "Never" to keep automation running regardless of login activity.
        </p>
      </div>

      <div>
        <div className="flex items-center justify-between">
          <div>
            <p className="text-sm font-medium text-gray-700">Auto-schedule AI posts</p>
            <p className="text-xs text-gray-400 mt-0.5">
              When on, AI-generated posts are automatically approved and queued for posting.
              When off, each post waits for your manual approval in the Review tab.
            </p>
          </div>
          <button
            type="button"
            onClick={() => setAutoSchedule((v) => !v)}
            className={`relative inline-flex h-6 w-11 flex-shrink-0 cursor-pointer rounded-full border-2 border-transparent transition-colors duration-200 focus:outline-none focus:ring-2 focus:ring-blue-500 focus:ring-offset-2 ${
              autoSchedule ? 'bg-blue-600' : 'bg-gray-200'
            }`}
            role="switch"
            aria-checked={autoSchedule}
          >
            <span
              className={`pointer-events-none inline-block h-5 w-5 transform rounded-full bg-white shadow ring-0 transition duration-200 ease-in-out ${
                autoSchedule ? 'translate-x-5' : 'translate-x-0'
              }`}
            />
          </button>
        </div>
      </div>

      <div>
        <label className="block text-sm font-medium text-gray-700 mb-1">
          Content language
        </label>
        <select
          value={contentLanguage}
          onChange={(e) => setContentLanguage(e.target.value)}
          className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
        >
          <option value="">
            Auto — from my Login Location
            {settingsData?.preferences?.effective_content_language
              ? ` (${settingsData.preferences.effective_content_language})`
              : ''}
          </option>
          {CONTENT_LANGUAGE_OPTIONS.map(({ label, value }) => (
            <option key={value} value={value}>
              {label}
            </option>
          ))}
        </select>
        <p className="text-xs text-gray-400 mt-1">
          The language your generated content is produced in — including the audio on premium
          (Veo) videos, which otherwise picks its own language. Leave on "Auto" to follow the
          country in your Login Location.
        </p>
      </div>

      {prefsSavedMsg && (
        <p className={`text-sm font-medium ${prefsMutation.isError ? 'text-red-600' : 'text-green-600'}`}>
          {prefsSavedMsg}
        </p>
      )}

      <button
        type="submit"
        disabled={prefsMutation.isPending}
        className="w-full bg-blue-600 text-white py-2 rounded-lg text-sm font-semibold hover:bg-blue-700 disabled:opacity-50 transition-colors"
      >
        {prefsMutation.isPending ? 'Saving…' : 'Save Preferences'}
      </button>
    </form>
  )
}
