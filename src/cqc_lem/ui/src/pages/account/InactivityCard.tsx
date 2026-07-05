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

export default function InactivityCard() {
  const { sessionToken } = useAuth()
  const [inactivateDelay, setInactivateDelay] = useState<number | null>(90)
  const [autoSchedule, setAutoSchedule] = useState(false)
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
      setPrefsInitialised(true)
    }
  }, [settingsData, prefsInitialised])

  const prefsMutation = useMutation({
    mutationFn: () =>
      api.put('/user/settings', {
        session_token: sessionToken,
        last_login_inactivate_delay: inactivateDelay,
        auto_schedule_posts: autoSchedule,
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
