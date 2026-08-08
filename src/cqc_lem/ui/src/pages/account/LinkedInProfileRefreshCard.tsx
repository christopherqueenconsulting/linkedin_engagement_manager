import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import api from '../../api/client'
import { useAuth } from '../../contexts/AuthContext'
import SettingsCard from '../../components/SettingsCard'

type ProfilePayload = {
  linkedin_profile_url: string | null
  // Seconds until the next refresh is allowed; 0 means "press it now".
  refresh_available_in_seconds?: number
}

type RefreshResult = {
  queued: boolean
  reason: string
  retry_after_seconds: number
}

// "in about 4 hours" reads better than a countdown nobody watches, and the window is a whole day.
function waitText(seconds: number): string {
  const hours = Math.ceil(seconds / 3600)
  if (hours <= 1) return 'in under an hour'
  return `in about ${hours} hours`
}

/**
 * Re-scrape my LinkedIn profile NOW (issue #1076).
 *
 * A profile edit — reordered skills, a rewritten headline — otherwise reaches LEM's writing only
 * when the weekly staleness beat catches up, so up to 7 days of content can be generated from the
 * old profile. This button collapses that to one browser session.
 *
 * The disabled state is driven by the SERVER's window (`refresh_available_in_seconds`), not by
 * local state, so it survives a reload: a user who presses the button, closes the tab and comes
 * back sees the same "already refreshed today" rather than a button that lies about being armed.
 */
export default function LinkedInProfileRefreshCard() {
  const { sessionToken } = useAuth()
  const queryClient = useQueryClient()
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null)

  // Deliberately NOT the Dashboard's ['linkedin-profile'] key: that query unwraps the payload to a
  // bare URL string, and two queries sharing a key must not disagree about the shape they cache.
  const { data } = useQuery({
    queryKey: ['linkedin-profile-refresh', sessionToken],
    queryFn: () =>
      api
        .get(`/user/linkedin-profile?session_token=${encodeURIComponent(sessionToken!)}`)
        .then((r) => r.data.detail as ProfilePayload),
    enabled: !!sessionToken,
    staleTime: 60 * 1000,
  })

  const waitSeconds = data?.refresh_available_in_seconds ?? 0
  const spent = waitSeconds > 0

  const mutation = useMutation({
    mutationFn: () =>
      api
        .post('/user/linkedin-profile/refresh', { session_token: sessionToken })
        .then((r) => r.data.detail as RefreshResult),
    onSuccess: (result) => {
      // 202 either way — a spent window is an expected no-op, not an error.
      queryClient.invalidateQueries({ queryKey: ['linkedin-profile-refresh'] })
      setMsg(
        result.queued
          ? { ok: true, text: 'Refreshing — your new profile reaches LEM within a few minutes.' }
          : {
              ok: false,
              text: `Already refreshed today — you can refresh again ${waitText(result.retry_after_seconds)}.`,
            },
      )
      setTimeout(() => setMsg(null), 8000)
    },
    onError: () => {
      setMsg({ ok: false, text: 'Could not start the refresh — please try again.' })
      setTimeout(() => setMsg(null), 5000)
    },
  })

  return (
    <SettingsCard
      title="Your LinkedIn Profile Data"
      subtitle="LEM writes in your voice from a cached copy of your profile. Edited your headline, skills or experience? Refresh so the next post and comment use the new version instead of waiting up to a week."
    >
      {msg && (
        <p className={`text-sm font-medium ${msg.ok ? 'text-green-600' : 'text-amber-700'}`}>{msg.text}</p>
      )}

      <button
        type="button"
        data-testid="refresh-profile-data"
        onClick={() => mutation.mutate()}
        disabled={mutation.isPending || spent}
        className="w-full bg-blue-600 text-white py-2 rounded-lg text-sm font-semibold hover:bg-blue-700 disabled:opacity-50 transition-colors"
      >
        {mutation.isPending
          ? 'Starting…'
          : spent
            ? `Refreshed today — again ${waitText(waitSeconds)}`
            : 'Refresh my profile data'}
      </button>

      <p className="text-xs text-gray-500">
        One refresh a day. It opens a browser session against LinkedIn, so it is deliberately not
        something to press repeatedly.
      </p>
    </SettingsCard>
  )
}
