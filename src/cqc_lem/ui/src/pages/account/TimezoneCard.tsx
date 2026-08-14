import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import api from '../../api/client'
import { useAuth } from '../../contexts/useAuth'

const COMMON_TIMEZONES = [
  'America/New_York',
  'America/Chicago',
  'America/Denver',
  'America/Los_Angeles',
  'America/Phoenix',
  'America/Anchorage',
  'Pacific/Honolulu',
  'America/Toronto',
  'America/Vancouver',
  'America/Sao_Paulo',
  'America/Mexico_City',
  'Europe/London',
  'Europe/Paris',
  'Europe/Berlin',
  'Europe/Amsterdam',
  'Europe/Rome',
  'Europe/Madrid',
  'Europe/Moscow',
  'Asia/Dubai',
  'Asia/Kolkata',
  'Asia/Singapore',
  'Asia/Tokyo',
  'Asia/Shanghai',
  'Australia/Sydney',
  'Pacific/Auckland',
  'UTC',
]

export default function TimezoneCard() {
  const { sessionToken } = useAuth()
  const queryClient = useQueryClient()
  // The user's own choice, once they make one — null means "show whatever the API says". Derived
  // rather than seeded in an effect so a refetch can never clobber an edit in progress.
  const [tzEdit, setTzEdit] = useState<string | null>(null)
  const [tzSavedMsg, setTzSavedMsg] = useState<string | null>(null)

  const { data: tzData } = useQuery({
    queryKey: ['user-timezone', sessionToken],
    queryFn: () =>
      api
        .get(`/user/timezone?session_token=${encodeURIComponent(sessionToken!)}`)
        .then((r) => r.data.detail as { timezone: string }),
    enabled: !!sessionToken,
    staleTime: 5 * 60 * 1000,
  })

  const timezone = tzEdit ?? tzData?.timezone ?? 'America/New_York'

  const tzMutation = useMutation({
    mutationFn: () =>
      api.put('/user/timezone', { session_token: sessionToken, timezone }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['user-timezone'] })
      setTzSavedMsg('Timezone saved!')
      setTimeout(() => setTzSavedMsg(null), 3000)
    },
    onError: () => {
      setTzSavedMsg('Save failed — please try again.')
      setTimeout(() => setTzSavedMsg(null), 5000)
    },
  })

  return (
    <form
      onSubmit={(e) => { e.preventDefault(); tzMutation.mutate() }}
      className="bg-white rounded-lg shadow-sm border border-gray-200 p-6 space-y-4"
    >
      <h2 className="text-base font-semibold text-gray-700">Timezone</h2>
      <div>
        <label className="block text-sm font-medium text-gray-700 mb-1">Your timezone</label>
        <select
          value={timezone}
          onChange={(e) => setTzEdit(e.target.value)}
          className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
        >
          {COMMON_TIMEZONES.map((tz) => (
            <option key={tz} value={tz}>{tz}</option>
          ))}
        </select>
        <p className="text-xs text-gray-400 mt-1">
          Scheduled post times will be displayed in this timezone across all pages.
        </p>
      </div>

      {tzSavedMsg && (
        <p className={`text-sm font-medium ${tzMutation.isError ? 'text-red-600' : 'text-green-600'}`}>
          {tzSavedMsg}
        </p>
      )}

      <button
        type="submit"
        disabled={tzMutation.isPending}
        className="w-full bg-blue-600 text-white py-2 rounded-lg text-sm font-semibold hover:bg-blue-700 disabled:opacity-50 transition-colors"
      >
        {tzMutation.isPending ? 'Saving…' : 'Save Timezone'}
      </button>
    </form>
  )
}
