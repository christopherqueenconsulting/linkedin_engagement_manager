import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import api from '../../api/client'
import { useAuth } from '../../contexts/AuthContext'
import SettingsCard from '../../components/SettingsCard'

export default function LoginLocationCard() {
  const { sessionToken } = useAuth()
  const queryClient = useQueryClient()
  const [locationMsg, setLocationMsg] = useState<{ ok: boolean; text: string } | null>(null)
  const [locCity, setLocCity] = useState('')
  const [locState, setLocState] = useState('')
  const [locCountry, setLocCountry] = useState('US')

  // Login location — used so the automation browser appears to log in from where
  // you normally do, reducing LinkedIn "new location" challenges.
  const { data: locationData } = useQuery({
    queryKey: ['user-location', sessionToken],
    queryFn: () =>
      api
        .get(`/user/location?session_token=${encodeURIComponent(sessionToken!)}`)
        .then((r) => r.data.detail as {
          latitude?: number; longitude?: number; city?: string; country?: string; timezone?: string
        }),
    enabled: !!sessionToken,
  })

  const autocaptureLocationMutation = useMutation({
    mutationFn: () =>
      api
        .post('/user/location/autocapture', { session_token: sessionToken })
        .then((r) => r.data.detail),
    onSuccess: (d: { city?: string; country?: string }) => {
      queryClient.invalidateQueries({ queryKey: ['user-location'] })
      queryClient.invalidateQueries({ queryKey: ['user-timezone'] })
      const where = [d?.city, d?.country].filter(Boolean).join(', ')
      setLocationMsg({ ok: true, text: where ? `Location set to ${where}.` : 'Location detected.' })
      setTimeout(() => setLocationMsg(null), 4000)
    },
    onError: () => {
      setLocationMsg({ ok: false, text: 'Could not detect your location — try again.' })
      setTimeout(() => setLocationMsg(null), 5000)
    },
  })

  const setLocationByCityMutation = useMutation({
    mutationFn: () =>
      api
        .post('/user/location/by-city', {
          session_token: sessionToken,
          city: locCity.trim(),
          state: locState.trim() || null,
          country: locCountry.trim() || null,
        })
        .then((r) => r.data.detail),
    onSuccess: (d: { city?: string; country?: string; timezone?: string }) => {
      queryClient.invalidateQueries({ queryKey: ['user-location'] })
      queryClient.invalidateQueries({ queryKey: ['user-timezone'] })
      const where = [d?.city, d?.country].filter(Boolean).join(', ')
      setLocationMsg({ ok: true, text: `Location set to ${where}${d?.timezone ? ` (${d.timezone})` : ''}.` })
      setTimeout(() => setLocationMsg(null), 4000)
    },
    onError: (e: any) => {
      const detail = e?.response?.data?.detail
      setLocationMsg({ ok: false, text: typeof detail === 'string' ? detail : 'Could not set that location — check the city/state.' })
      setTimeout(() => setLocationMsg(null), 5000)
    },
  })

  return (
    <SettingsCard
      title="Login Location"
      subtitle={'The automation logs into LinkedIn from our servers. Setting your location makes the browser appear to come from where you normally log in, which reduces LinkedIn "new location" security challenges.'}
    >
      <div className="text-sm text-gray-700">
        <span className="font-medium text-gray-600">Current: </span>
        {locationData?.latitude != null
          ? `${[locationData.city, locationData.country].filter(Boolean).join(', ') || 'Set'} (${locationData.latitude.toFixed(3)}, ${locationData.longitude?.toFixed(3)})`
          : 'Not set'}
      </div>

      {locationMsg && (
        <p className={`text-sm font-medium ${locationMsg.ok ? 'text-green-600' : 'text-red-600'}`}>
          {locationMsg.text}
        </p>
      )}

      {/* Manual city/state entry */}
      <div className="grid grid-cols-1 sm:grid-cols-3 gap-2">
        <input
          type="text"
          value={locCity}
          onChange={(e) => setLocCity(e.target.value)}
          placeholder="City"
          className="border border-gray-300 rounded-lg px-3 py-2 text-sm sm:col-span-1"
        />
        <input
          type="text"
          value={locState}
          onChange={(e) => setLocState(e.target.value)}
          placeholder="State / region"
          className="border border-gray-300 rounded-lg px-3 py-2 text-sm sm:col-span-1"
        />
        <input
          type="text"
          value={locCountry}
          onChange={(e) => setLocCountry(e.target.value)}
          placeholder="Country (ISO-2)"
          maxLength={2}
          className="border border-gray-300 rounded-lg px-3 py-2 text-sm uppercase sm:col-span-1"
        />
      </div>
      <button
        type="button"
        onClick={() => setLocationByCityMutation.mutate()}
        disabled={setLocationByCityMutation.isPending || !locCity.trim()}
        className="w-full bg-blue-600 text-white py-2 rounded-lg text-sm font-semibold hover:bg-blue-700 disabled:opacity-50 transition-colors"
      >
        {setLocationByCityMutation.isPending ? 'Setting…' : 'Set location'}
      </button>

      <div className="relative py-1">
        <div className="border-t border-gray-200" />
        <span className="absolute inset-0 -top-2 flex justify-center text-xs text-gray-400">
          <span className="bg-white px-2">or</span>
        </span>
      </div>

      <button
        type="button"
        onClick={() => autocaptureLocationMutation.mutate()}
        disabled={autocaptureLocationMutation.isPending}
        className="w-full bg-gray-100 text-gray-700 py-2 rounded-lg text-sm font-semibold hover:bg-gray-200 disabled:opacity-50 transition-colors"
      >
        {autocaptureLocationMutation.isPending ? 'Detecting…' : 'Use my current location (auto-detect)'}
      </button>
    </SettingsCard>
  )
}
