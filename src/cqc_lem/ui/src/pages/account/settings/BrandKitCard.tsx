import CsvInput from './CsvInput'
import { useEngagementPrefs } from './engagementPrefsCtx'
import { Field, SectionCard, inputClass } from './Field'
import { BRAND_COLORS, BRAND_KIT_LIMITS, type BrandKit, isValidHex, mergeKit } from './brandKit'

// The brand kit rides the engagement-prefs row's GET/PUT, but the server stores it in its own
// column and re-validates every field — a bad hex is dropped there, never a failed save — so this
// card only flags it and moves on.
export default function BrandKitCard() {
  const { eng, setEng } = useEngagementPrefs()
  if (!eng) return null
  const kit = (eng.brand_kit ?? {}) as BrandKit
  const update = (patch: Partial<BrandKit>) => setEng({ brand_kit: mergeKit(kit, patch) })

  return (
    <SectionCard
      title="Brand kit"
      blurb="Colours, type feel and mood that AI images for your posts and newsletter covers are steered toward. Leave it empty for neutral colour grading."
    >
      <Field settingKey="brand_kit_colors">
        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3">
          {BRAND_COLORS.map(({ key, label }) => {
            const value = kit[key] ?? ''
            const valid = isValidHex(value)
            const swatch = valid ? `#${value.trim().replace(/^#/, '').toLowerCase()}` : '#ffffff'
            return (
              <div key={key} data-testid={`brand-color-${key}`}>
                <span className="block text-xs font-medium text-gray-600 mb-1">{label}</span>
                <div className="flex items-center gap-2">
                  <input type="color" value={swatch} aria-label={`${label} colour picker`}
                    onChange={(e) => update({ [key]: e.target.value } as Partial<BrandKit>)}
                    className="h-9 w-10 shrink-0 cursor-pointer rounded border border-gray-300 bg-white p-0.5" />
                  <input type="text" value={value} maxLength={7} placeholder="#rrggbb"
                    aria-label={`${label} hex`}
                    onChange={(e) => update({ [key]: e.target.value.trim() } as Partial<BrandKit>)}
                    className={`${inputClass} font-mono ${value && !valid ? 'border-red-400' : ''}`} />
                </div>
                {value && !valid && (
                  <p className="text-xs text-red-600 mt-1">Not a #rrggbb colour — it will not be saved.</p>
                )}
              </div>
            )
          })}
        </div>
      </Field>
      <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
        <Field settingKey="brand_kit_font_vibe">
          <input type="text" value={kit.font_vibe ?? ''} maxLength={BRAND_KIT_LIMITS.font_vibe}
            onChange={(e) => update({ font_vibe: e.target.value })}
            placeholder="e.g. clean geometric sans, bold headlines" className={inputClass} />
        </Field>
        <Field settingKey="brand_kit_avoid">
          <CsvInput value={kit.avoid ?? []}
            onChange={(values) => update({ avoid: values.slice(0, BRAND_KIT_LIMITS.avoid_items) })}
            placeholder="e.g. gears, lightbulbs, robots" />
        </Field>
      </div>
      <Field settingKey="brand_kit_visual_mood">
        <textarea value={kit.visual_mood ?? ''} rows={2} maxLength={BRAND_KIT_LIMITS.visual_mood}
          onChange={(e) => update({ visual_mood: e.target.value })}
          placeholder="e.g. practitioner field notes: real work, warm light, confident and specific"
          className={inputClass} />
      </Field>
    </SectionCard>
  )
}
