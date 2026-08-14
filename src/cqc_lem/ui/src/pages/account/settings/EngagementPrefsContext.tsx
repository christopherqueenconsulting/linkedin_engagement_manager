import { useMemo, useState, type ReactNode } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import api from '../../../api/client'
import { useAuth } from '../../../contexts/useAuth'
import type { EngPrefs } from '../types'
import { EngagementPrefsCtx, type EngCtx } from './engagementPrefsCtx'
import { useRegisterSaveSection } from '../settingsSave'
import { blockingIssues } from './conflicts'
import { DEFAULT_PRESET, presetValues } from './presets'

export function EngagementPrefsProvider({ children }: { children: ReactNode }) {
  const { sessionToken } = useAuth()
  const queryClient = useQueryClient()
  // Only the fields the user has touched, laid over the API row at render time — so nothing is
  // seeded in an effect, and a background refetch can never overwrite an edit in progress.
  const [edits, setEdits] = useState<Partial<EngPrefs> | null>(null)
  const [message, setMessage] = useState<{ ok: boolean; text: string } | null>(null)

  const { data } = useQuery({
    queryKey: ['engagement-preferences', sessionToken],
    queryFn: () =>
      api
        .get(`/user/engagement-preferences?session_token=${encodeURIComponent(sessionToken!)}`)
        .then((r) => r.data.detail as EngPrefs),
    enabled: !!sessionToken,
    staleTime: 60 * 1000,
  })

  // A brand-new account starts on Balanced (issue #558, decision 2A). It is applied as an UNSAVED
  // change — nothing is written until the user saves — so an existing user's stored values can
  // never be overwritten by this, and nobody's outbound posture changes silently.
  const isNewAccount = data?.has_saved_preferences === false
  const preset = useMemo(
    () => (data && isNewAccount ? presetValues(DEFAULT_PRESET, data.max_catchup_touches_allowed ?? 5) : null),
    [data, isNewAccount]
  )
  // What the row would be if the user saved right now: the API row, the new-account preset over it,
  // then their own edits.
  const eng: EngPrefs | null = useMemo(
    () => (data ? { ...data, ...preset, ...edits } : null),
    [data, preset, edits]
  )
  const presetApplied = preset !== null

  const setEng = (patch: Partial<EngPrefs>) => setEdits((p) => ({ ...p, ...patch }))

  const mutation = useMutation({
    mutationFn: () => api.put('/user/engagement-preferences', { session_token: sessionToken, ...eng }),
    onSuccess: async () => {
      // Only once the refetch has ANSWERED — until then React Query still serves the pre-save row,
      // so clearing the edits first would put every field (and, on a first save, the Balanced
      // preset) back on screen for the length of the round trip.
      await queryClient.invalidateQueries({ queryKey: ['engagement-preferences'] })
      setEdits(null)
      setMessage({ ok: true, text: 'Saved.' })
      setTimeout(() => setMessage(null), 3000)
    },
    onError: () => {
      setMessage({ ok: false, text: 'Could not save — try again.' })
      setTimeout(() => setMessage(null), 5000)
    },
  })

  const isDirty = !!data && JSON.stringify(eng) !== JSON.stringify(data)

  const save = async (): Promise<boolean> => {
    if (!eng) return false
    // Refuse a write that MySQL would reject: the whole row is one statement, so an over-long value
    // rolls back every other section's changes with it (the V52 incident).
    const blocked = blockingIssues(eng)
    if (blocked.length > 0) {
      setMessage({ ok: false, text: `Fix ${blocked.length} invalid value${blocked.length === 1 ? '' : 's'} before saving.` })
      setTimeout(() => setMessage(null), 6000)
      return false
    }
    await mutation.mutateAsync()
    return true
  }

  useRegisterSaveSection('engagement', 'Voice, targeting & caps', isDirty, save)

  const value = useMemo<EngCtx>(
    () => ({
      eng, setEng, isDirty, saving: mutation.isPending, message, save,
      catchupAllowed: eng?.max_catchup_touches_allowed ?? 5,
      isNewAccount, presetApplied,
    }),
    [eng, isDirty, mutation.isPending, message, isNewAccount, presetApplied]
  )
  return <EngagementPrefsCtx.Provider value={value}>{children}</EngagementPrefsCtx.Provider>
}
