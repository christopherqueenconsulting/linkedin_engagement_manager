// Shape and bounds of the dual-audience setting (utilities/ai/audience_mix.py). Kept apart from
// AudienceMixCard.tsx so that file exports a component only (Fast Refresh).
export type AudienceMix = {
  primary_audience?: string
  secondary_audience?: string
  // The secondary audience's share of posts over any ten, 0-1. 0 = single audience.
  secondary_share?: number
  secondary_focus_topics?: string[]
}

// Kept in lockstep with AUDIENCE_DESCRIPTION_MAX / AUDIENCE_TOPICS_MAX in audience_mix.py.
export const AUDIENCE_MIX_LIMITS = { description: 400, topics: 12 }

/** The stored share as a slider percentage (0-100). */
export const sharePercent = (mix: AudienceMix): number =>
  Math.round(Math.min(1, Math.max(0, mix.secondary_share ?? 0)) * 100)

/** A patch merged over the mix with empty values REMOVED, so a cleared field stops being sent. */
export function mergeMix(mix: AudienceMix, patch: Partial<AudienceMix>): AudienceMix {
  const next: AudienceMix = { ...mix, ...patch }
  for (const k of Object.keys(next) as (keyof AudienceMix)[]) {
    const v = next[k]
    if (v === undefined || v === '' || (Array.isArray(v) && v.length === 0)) delete next[k]
  }
  return next
}
