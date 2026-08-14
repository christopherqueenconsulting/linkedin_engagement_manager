import { createContext, useContext } from 'react'
import type { EngPrefs } from '../types'

// Voice, targeting, caps, connections and catch-up all live in ONE engagement_preferences row that
// is written by ONE INSERT … ON DUPLICATE KEY UPDATE. The hub renders them in five different
// sections, so they must share one piece of state and one mutation — otherwise saving one section
// would write the other sections' stale copies back over the user's edits.
// The context object and its hook sit here, apart from the provider component, so the provider's
// file exports a component only (Fast Refresh).
export type EngCtx = {
  eng: EngPrefs | null
  setEng: (patch: Partial<EngPrefs>) => void
  isDirty: boolean
  saving: boolean
  message: { ok: boolean; text: string } | null
  save: () => Promise<boolean>
  catchupAllowed: number
  /** True until the user's first save — the hub starts these accounts on the Balanced preset. */
  isNewAccount: boolean
  presetApplied: boolean
}

export const EngagementPrefsCtx = createContext<EngCtx | null>(null)

export function useEngagementPrefs(): EngCtx {
  const ctx = useContext(EngagementPrefsCtx)
  if (!ctx) throw new Error('useEngagementPrefs must be used inside an EngagementPrefsProvider')
  return ctx
}
