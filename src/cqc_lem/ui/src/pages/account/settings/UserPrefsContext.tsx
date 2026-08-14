import { useEffect, useMemo, useState, type ReactNode } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import api from '../../../api/client'
import { useAuth } from '../../../contexts/useAuth'
import type { UserPrefs } from '../types'
import { UserPrefsCtxObject, type UserPrefsCtx } from './userPrefsCtx'
import { useRegisterSaveSection } from '../settingsSave'

export type UserSettingsResponse = {
  subscription: unknown
  preferences: (UserPrefs & { effective_content_language: string | null }) | null
  blog_url: string | null
  sitemap_url: string | null
  company_linked_in_url: string | null
}

export function UserPrefsProvider({ children }: { children: ReactNode }) {
  const { sessionToken } = useAuth()
  const queryClient = useQueryClient()
  const [prefs, setPrefsState] = useState<UserPrefs | null>(null)
  const [savedSig, setSavedSig] = useState<string | null>(null)
  const [message, setMessage] = useState<{ ok: boolean; text: string } | null>(null)

  const { data } = useQuery({
    queryKey: ['user-settings', sessionToken],
    queryFn: () =>
      api
        .get(`/user/settings?session_token=${encodeURIComponent(sessionToken!)}`)
        .then((r) => r.data.detail as UserSettingsResponse),
    enabled: !!sessionToken,
    staleTime: 60 * 1000,
  })

  useEffect(() => {
    if (!data?.preferences || prefs) return
    const loaded: UserPrefs = {
      last_login_inactivate_delay: data.preferences.last_login_inactivate_delay,
      auto_schedule_posts: data.preferences.auto_schedule_posts,
      content_language: data.preferences.content_language,
      content_buffer_days: data.preferences.content_buffer_days,
      content_buffer_max_posts: data.preferences.content_buffer_max_posts,
    }
    setPrefsState(loaded)
    setSavedSig(JSON.stringify(loaded))
  }, [data, prefs])

  const setPrefs = (patch: Partial<UserPrefs>) => setPrefsState((p) => (p ? { ...p, ...patch } : p))

  const mutation = useMutation({
    mutationFn: () =>
      api.put('/user/settings', {
        session_token: sessionToken,
        last_login_inactivate_delay: prefs?.last_login_inactivate_delay ?? null,
        auto_schedule_posts: prefs?.auto_schedule_posts ?? false,
        content_language: prefs?.content_language ?? '',
        content_buffer_days: prefs?.content_buffer_days,
        content_buffer_max_posts: prefs?.content_buffer_max_posts,
      }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['user-settings'] })
      setSavedSig(JSON.stringify(prefs))
      setMessage({ ok: true, text: 'Saved.' })
      setTimeout(() => setMessage(null), 3000)
    },
    onError: () => {
      setMessage({ ok: false, text: 'Could not save — try again.' })
      setTimeout(() => setMessage(null), 5000)
    },
  })

  const isDirty = !!prefs && savedSig !== null && JSON.stringify(prefs) !== savedSig

  // Never write preferences we have not loaded — that is what let a save reset auto-scheduling.
  const save = async (): Promise<boolean> => {
    if (!prefs) return false
    await mutation.mutateAsync()
    return true
  }

  useRegisterSaveSection('user-prefs', 'Account preferences', isDirty, save)

  const value = useMemo<UserPrefsCtx>(
    () => ({
      prefs,
      effectiveLanguage: data?.preferences?.effective_content_language ?? null,
      setPrefs, isDirty, saving: mutation.isPending, message, save,
    }),
    [prefs, data, isDirty, mutation.isPending, message]
  )
  return <UserPrefsCtxObject.Provider value={value}>{children}</UserPrefsCtxObject.Provider>
}
