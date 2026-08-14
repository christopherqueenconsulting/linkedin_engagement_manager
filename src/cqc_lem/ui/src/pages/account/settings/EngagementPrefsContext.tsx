import { useEffect, useMemo, useState, type ReactNode } from 'react'
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
  const [eng, setEngState] = useState<EngPrefs | null>(null)
  const [savedSig, setSavedSig] = useState<string | null>(null)
  const [message, setMessage] = useState<{ ok: boolean; text: string } | null>(null)
  const [isNewAccount, setIsNewAccount] = useState(false)
  const [presetApplied, setPresetApplied] = useState(false)

  const { data } = useQuery({
    queryKey: ['engagement-preferences', sessionToken],
    queryFn: () =>
      api
        .get(`/user/engagement-preferences?session_token=${encodeURIComponent(sessionToken!)}`)
        .then((r) => r.data.detail as EngPrefs),
    enabled: !!sessionToken,
    staleTime: 60 * 1000,
  })

  useEffect(() => {
    if (!data || eng) return
    const fresh = data.has_saved_preferences === false
    setSavedSig(JSON.stringify(data))
    setIsNewAccount(fresh)
    if (fresh) {
      // A brand-new account starts on Balanced (issue #558, decision 2A). It is applied as an
      // UNSAVED change — nothing is written until the user saves — so an existing user's stored
      // values can never be overwritten by this, and nobody's outbound posture changes silently.
      const allowed = data.max_catchup_touches_allowed ?? 5
      setEngState({ ...data, ...presetValues(DEFAULT_PRESET, allowed) })
      setPresetApplied(true)
    } else {
      setEngState(data)
    }
  }, [data, eng])

  const setEng = (patch: Partial<EngPrefs>) => setEngState((p) => (p ? { ...p, ...patch } : p))

  const mutation = useMutation({
    mutationFn: () => api.put('/user/engagement-preferences', { session_token: sessionToken, ...eng }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['engagement-preferences'] })
      setSavedSig(JSON.stringify(eng))
      setIsNewAccount(false)
      setPresetApplied(false)
      setMessage({ ok: true, text: 'Saved.' })
      setTimeout(() => setMessage(null), 3000)
    },
    onError: () => {
      setMessage({ ok: false, text: 'Could not save — try again.' })
      setTimeout(() => setMessage(null), 5000)
    },
  })

  const isDirty = !!eng && savedSig !== null && JSON.stringify(eng) !== savedSig

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
