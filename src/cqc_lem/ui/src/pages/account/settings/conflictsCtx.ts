import { createContext, useContext } from 'react'
import type { Finding } from './conflicts'

// The context object and its hook sit here, apart from the provider component, so the provider's
// file exports a component only (Fast Refresh).
export type ConflictsCtx = {
  findings: Finding[]
  alertCounts: Record<string, number>
  applyFix: (finding: Finding) => void
}

export const ConflictsCtxObject = createContext<ConflictsCtx | null>(null)

// Outside a provider there are no findings — the hub's sections stay usable on their own.
export function useConflicts(): ConflictsCtx {
  return useContext(ConflictsCtxObject) ?? { findings: [], alertCounts: {}, applyFix: () => {} }
}
