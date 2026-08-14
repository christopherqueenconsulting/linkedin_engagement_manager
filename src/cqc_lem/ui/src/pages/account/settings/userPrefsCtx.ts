import { createContext, useContext } from 'react'
import type { UserPrefs } from '../types'

// PUT /user/settings writes the whole preferences object at once, so the inactivity auto-stop
// (Setup), auto-schedule + content buffer (Content & Publishing) and content language (My Voice)
// must share one state object and one mutation even though the hub renders them three sections
// apart. This also fixes F11: the old card seeded `auto_schedule_posts=false` / 90 days BEFORE the
// API answered, so saving anything else could quietly turn auto-scheduling off.
// The context object and its hook sit here, apart from the provider component, so the provider's
// file exports a component only (Fast Refresh).
export type UserPrefsCtx = {
  prefs: UserPrefs | null
  effectiveLanguage: string | null
  setPrefs: (patch: Partial<UserPrefs>) => void
  isDirty: boolean
  saving: boolean
  message: { ok: boolean; text: string } | null
  save: () => Promise<boolean>
}

export const UserPrefsCtxObject = createContext<UserPrefsCtx | null>(null)

export function useUserPrefs(): UserPrefsCtx {
  const ctx = useContext(UserPrefsCtxObject)
  if (!ctx) throw new Error('useUserPrefs must be used inside a UserPrefsProvider')
  return ctx
}
