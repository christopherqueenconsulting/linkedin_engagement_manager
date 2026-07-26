import { createContext, useContext, useMemo, type ReactNode } from 'react'
import { useQuery } from '@tanstack/react-query'
import api from '../../../api/client'
import { useAuth } from '../../../contexts/AuthContext'
import type { LeadMagnet, NewsletterSettings } from '../types'
import { evaluateConflicts, sectionAlertCounts, type Finding } from './conflicts'
import { useEngagementPrefs } from './EngagementPrefsContext'
import { useUserPrefs } from './UserPrefsContext'

type ConflictsCtx = {
  findings: Finding[]
  alertCounts: Record<string, number>
  applyFix: (finding: Finding) => void
}

const Ctx = createContext<ConflictsCtx | null>(null)

const browserTimezone = () => {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || null
  } catch {
    return null
  }
}

// Conflicts are evaluated against the LIVE engagement + account preferences (so a warning appears
// as you type) and the SAVED newsletter / lead-magnet / credit state, which the hub does not hold
// in shared state.
export function ConflictsProvider({ children }: { children: ReactNode }) {
  const { sessionToken } = useAuth()
  const { eng, setEng, catchupAllowed } = useEngagementPrefs()
  const { prefs } = useUserPrefs()

  const { data: newsletter } = useQuery({
    queryKey: ['newsletter-settings', sessionToken],
    queryFn: () =>
      api
        .get(`/user/newsletter-settings?session_token=${encodeURIComponent(sessionToken!)}`)
        .then((r) => r.data.detail as NewsletterSettings),
    enabled: !!sessionToken,
    staleTime: 60 * 1000,
  })

  const { data: leadMagnet } = useQuery({
    queryKey: ['lead-magnet', sessionToken],
    queryFn: () =>
      api
        .get(`/user/lead-magnet?session_token=${encodeURIComponent(sessionToken!)}`)
        .then((r) => r.data.detail as LeadMagnet),
    enabled: !!sessionToken,
    staleTime: 60 * 1000,
  })

  const { data: credits } = useQuery({
    queryKey: ['video-credits', sessionToken],
    queryFn: () =>
      api
        .get('/video/credits', { params: { session_token: sessionToken } })
        .then((r) => r.data.detail as { balance: number }),
    enabled: !!sessionToken,
  })

  const { data: tz } = useQuery({
    queryKey: ['user-timezone', sessionToken],
    queryFn: () =>
      api
        .get(`/user/timezone?session_token=${encodeURIComponent(sessionToken!)}`)
        .then((r) => r.data.detail as { timezone: string }),
    enabled: !!sessionToken,
    staleTime: 5 * 60 * 1000,
  })

  const findings = useMemo<Finding[]>(() => {
    if (!eng) return []
    return evaluateConflicts({
      eng,
      user: prefs,
      newsletter: newsletter ?? null,
      leadMagnet: leadMagnet ?? null,
      videoCredits: credits?.balance ?? 0,
      timezone: tz?.timezone ?? null,
      browserTimezone: browserTimezone(),
      catchupAllowed,
    })
  }, [eng, prefs, newsletter, leadMagnet, credits, tz, catchupAllowed])

  const value = useMemo<ConflictsCtx>(
    () => ({
      findings,
      alertCounts: sectionAlertCounts(findings),
      applyFix: (finding: Finding) => { if (finding.fix) setEng(finding.fix.patch) },
    }),
    // setEng is stable enough for this purpose (it only closes over the state setter).
    [findings] // eslint-disable-line react-hooks/exhaustive-deps
  )
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>
}

export function useConflicts(): ConflictsCtx {
  return useContext(Ctx) ?? { findings: [], alertCounts: {}, applyFix: () => {} }
}
