import type { EngPrefs } from '../types'

// Guided volume presets (issue #558 §7). These are NOT a second volume policy: they are exactly the
// outbound ramp already signed off for the brand account (utilities/brand_account.py
// PHASE_OUTBOUND_POLICY P0/P1/P2), so what we recommend to a customer is what we run ourselves. No
// preset may exceed the shipped per-user defaults (20 comments / 20 DMs / 10 invites) — the same
// ceiling rule BRAND_CAP_CEILINGS enforces server-side.
export type PresetKey = 'conservative' | 'balanced' | 'aggressive'

// The fields a preset owns. Anything not listed here is never touched by picking a preset.
export type PresetValues = Pick<
  EngPrefs,
  | 'max_comments_per_day'
  | 'max_dms_per_day'
  | 'max_invites_per_day'
  | 'connection_request_mode'
  | 'connection_targeting_mode'
  | 'max_catchup_touches_per_day'
  | 'catchup_touch_mode'
>

export type Preset = {
  key: PresetKey
  label: string
  blurb: string
  /** Catch-up cap this preset wants; clamped to the plan's ceiling by presetValues(). */
  values: PresetValues
}

export const PRESETS: Preset[] = [
  {
    key: 'conservative',
    label: 'Conservative',
    blurb: 'Warming up a newer account, or you want a human on every outbound touch.',
    values: {
      max_comments_per_day: 8,
      max_dms_per_day: 5,
      max_invites_per_day: 5,
      connection_request_mode: 'pre_review',
      connection_targeting_mode: 'suggest',
      max_catchup_touches_per_day: 2,
      catchup_touch_mode: 'pre_review',
    },
  },
  {
    key: 'balanced',
    label: 'Balanced',
    blurb: 'Steady daily presence with connects going out automatically. Recommended.',
    values: {
      max_comments_per_day: 15,
      max_dms_per_day: 10,
      max_invites_per_day: 8,
      connection_request_mode: 'auto_approve',
      connection_targeting_mode: 'auto_queue',
      max_catchup_touches_per_day: 5,
      catchup_touch_mode: 'pre_review',
    },
  },
  {
    key: 'aggressive',
    label: 'Aggressive',
    blurb: 'An established account pushing for maximum reach. Watch your reply quality.',
    values: {
      max_comments_per_day: 20,
      max_dms_per_day: 15,
      max_invites_per_day: 10,
      connection_request_mode: 'auto_approve',
      connection_targeting_mode: 'auto_queue',
      max_catchup_touches_per_day: 10,
      catchup_touch_mode: 'auto_approve',
    },
  },
]

export const DEFAULT_PRESET: PresetKey = 'balanced'

export const getPreset = (key: PresetKey): Preset =>
  PRESETS.find((p) => p.key === key) ?? PRESETS[1]

/**
 * A preset's values for THIS user — the catch-up cap is clamped to what their plan allows (10/day is
 * premium-only and the server clamps it on save, so an unclamped preset would look "Custom" the
 * moment it round-trips).
 */
export function presetValues(key: PresetKey, catchupAllowed: number): PresetValues {
  const preset = getPreset(key)
  return {
    ...preset.values,
    max_catchup_touches_per_day: Math.min(catchupAllowed, preset.values.max_catchup_touches_per_day),
  }
}

/** The preset these settings exactly match, or null for "Custom". Presets are never sticky: changing
 *  any single value they own flips the selector back to Custom. */
export function detectPreset(eng: EngPrefs, catchupAllowed: number): PresetKey | null {
  for (const preset of PRESETS) {
    const values = presetValues(preset.key, catchupAllowed)
    const matches = (Object.keys(values) as (keyof PresetValues)[]).every(
      (k) => eng[k] === values[k]
    )
    if (matches) return preset.key
  }
  return null
}
