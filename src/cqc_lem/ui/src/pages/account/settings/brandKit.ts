// Shape and bounds of the brand kit image generation reads (utilities/brand_kit.py). Kept apart from
// BrandKitCard.tsx so that file exports a component only (Fast Refresh).
export type BrandKit = {
  primary_hex?: string
  secondary_hex?: string
  accent_hex?: string
  neutral_dark_hex?: string
  neutral_light_hex?: string
  font_vibe?: string
  visual_mood?: string
  avoid?: string[]
}

type ColorKey = 'primary_hex' | 'secondary_hex' | 'accent_hex' | 'neutral_dark_hex' | 'neutral_light_hex'

export const BRAND_COLORS: { key: ColorKey; label: string }[] = [
  { key: 'primary_hex', label: 'Primary' },
  { key: 'secondary_hex', label: 'Secondary' },
  { key: 'accent_hex', label: 'Accent' },
  { key: 'neutral_dark_hex', label: 'Dark neutral' },
  { key: 'neutral_light_hex', label: 'Light neutral' },
]

// Kept in lockstep with FONT_VIBE_MAX / VISUAL_MOOD_MAX / AVOID_* in utilities/brand_kit.py.
export const BRAND_KIT_LIMITS = { font_vibe: 80, visual_mood: 160, avoid_items: 12, avoid_item: 40 }

const HEX_RE = /^#?[0-9a-fA-F]{6}$/

export const isValidHex = (v: string | undefined): boolean => !!v && HEX_RE.test(v.trim())

/** A patch with empty values REMOVED, so a cleared field stops being sent rather than sent blank. */
export function mergeKit(kit: BrandKit, patch: Partial<BrandKit>): BrandKit {
  const next: BrandKit = { ...kit, ...patch }
  for (const k of Object.keys(next) as (keyof BrandKit)[]) {
    const v = next[k]
    if (v === undefined || v === '' || (Array.isArray(v) && v.length === 0)) delete next[k]
  }
  return next
}
