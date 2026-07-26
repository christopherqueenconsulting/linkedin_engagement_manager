import {
  createContext, useContext, useEffect, useMemo, useState, type ReactNode,
} from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import api from '../../../api/client'
import { useAuth } from '../../../contexts/AuthContext'
import type { UserPrefs } from '../types'
import { useRegisterSaveSection } from '../SettingsSaveContext'

export type UserSettingsResponse = {
  subscription: unknown
  preferences: (UserPrefs & { effective_content_language: string | null }) | null
  blog_url: string | null
  sitemap_url: string | null
  company_linked_in_url: string | null
}

// PUT /user/settings writes the whole preferences object at once, so the inactivity auto-stop
// (Setup), auto-schedule + content buffer (Content & Publishing) and content language (My Voice)
// must share one state object and one mutation even though the hub renders them three sections
// apart. This also fixes F11: the old card seeded `auto_schedule_posts=false` / 90 days BEFORE the
// API answered, so saving anything else could quietly turn auto-scheduling off.
type UserPrefsCtx = {
  prefs: UserPrefs | null
  effectiveLanguage: string | null
  setPrefs: (patch: Partial<UserPrefs>) => void
  isDirty: boolean
  saving: boolean
  message: { ok: boolean; text: string } | null
  save: () => Promise<boolean>
}

const Ctx = createContext<UserPrefsCtx | null>(null)

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
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>
}

export function useUserPrefs(): UserPrefsCtx {
  const ctx = useContext(Ctx)
  if (!ctx) throw new Error('useUserPrefs must be used inside a UserPrefsProvider')
  return ctx
}
