import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import api from '../../api/client'
import { useAuth } from '../../contexts/useAuth'
import SettingsCard from '../../components/SettingsCard'

type DisplayNameResponse = {
  linkedin_display_name: string | null
  profile_full_name: string | null
}

/**
 * Your name as LinkedIn writes it on your own messages (issue #731) — REQUIRED.
 *
 * Reply detection asks one question of every DM thread: "is the last message from us?", and the
 * only thing it can compare is the sender name LinkedIn renders. With no name saved the answer is
 * UNKNOWN, and LEM skips the follow-up rather than risk messaging someone who already replied — so
 * an empty field silently stops the whole sequence. Hence: one field, required, and explicit that
 * it must match the profile exactly (no separate first/last — LinkedIn labels the message group
 * with the full display name as one string).
 */
export default function LinkedInDisplayNameCard() {
  const { sessionToken } = useAuth()
  const queryClient = useQueryClient()
  // null = untouched, so the field shows the fetched value; anything typed WINS over a late fetch
  // (an effect that seeded state on arrival would wipe a name the user was already typing).
  const [typed, setTyped] = useState<string | null>(null)
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null)

  const { data } = useQuery({
    queryKey: ['linkedin-display-name', sessionToken],
    queryFn: () =>
      api
        .get(`/user/linkedin-display-name?session_token=${encodeURIComponent(sessionToken!)}`)
        .then((r) => r.data.detail as DisplayNameResponse),
    enabled: !!sessionToken,
    staleTime: 60 * 1000,
  })

  const saved = (data?.linkedin_display_name ?? '').trim()
  const scraped = (data?.profile_full_name ?? '').trim()
  const name = typed ?? (data?.linkedin_display_name ?? '')
  const trimmed = name.trim()
  const loaded = !!data

  const mutation = useMutation({
    mutationFn: () =>
      api.put('/user/linkedin-display-name', {
        session_token: sessionToken,
        linkedin_display_name: trimmed,
      }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['linkedin-display-name'] })
      queryClient.invalidateQueries({ queryKey: ['account-readiness'] })
      setMsg({ ok: true, text: 'Name saved.' })
      setTimeout(() => setMsg(null), 4000)
    },
    onError: () => {
      setMsg({ ok: false, text: 'Save failed — please try again.' })
      setTimeout(() => setMsg(null), 5000)
    },
  })

  return (
    <SettingsCard
      title="Your LinkedIn Name"
      subtitle="Type your name exactly as it appears at the top of your LinkedIn profile — same spelling, accents and capitalisation LinkedIn shows on your messages. One field: your full display name, not separate first and last names."
      headerRight={
        <span className="text-[11px] font-semibold text-red-600 bg-red-50 px-2 py-0.5 rounded whitespace-nowrap">
          Required
        </span>
      }
    >
      <p className="text-xs text-gray-500">
        LEM uses this to tell your own messages apart from a reply. If it doesn't match what
        LinkedIn shows, LEM can't read the conversation and pauses that person's follow-ups instead
        of risking a message to someone who already answered.
      </p>

      <div>
        <label htmlFor="linkedin-display-name" className="block text-sm font-medium text-gray-700 mb-1">
          Full name on LinkedIn <span className="text-red-500">*</span>
        </label>
        <input
          id="linkedin-display-name"
          type="text"
          value={name}
          onChange={(e) => setTyped(e.target.value)}
          aria-required="true"
          aria-invalid={loaded && !trimmed}
          className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
          placeholder="e.g. Jordan Alvarez"
        />
        {loaded && !trimmed && (
          <p className="text-xs text-red-600 mt-1">
            Required — DM follow-ups stay paused until this is filled in.
          </p>
        )}
        {scraped && scraped !== trimmed && (
          <p className="text-xs text-gray-500 mt-1">
            Your scraped profile reads “{scraped}”.{' '}
            <button
              type="button"
              onClick={() => setTyped(scraped)}
              className="text-blue-600 font-medium hover:underline"
            >
              Use this
            </button>
          </p>
        )}
      </div>

      {msg && (
        <p className={`text-sm font-medium ${msg.ok ? 'text-green-600' : 'text-red-600'}`}>{msg.text}</p>
      )}

      <button
        type="button"
        onClick={() => mutation.mutate()}
        disabled={mutation.isPending || !trimmed || trimmed === saved}
        className="w-full bg-blue-600 text-white py-2 rounded-lg text-sm font-semibold hover:bg-blue-700 disabled:opacity-50 transition-colors"
      >
        {mutation.isPending ? 'Saving…' : 'Save Name'}
      </button>
    </SettingsCard>
  )
}
