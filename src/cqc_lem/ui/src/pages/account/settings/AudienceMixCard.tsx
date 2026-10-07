import CsvInput from './CsvInput'
import { useEngagementPrefs } from './engagementPrefsCtx'
import { Field, SectionCard, inputClass } from './Field'
import { AUDIENCE_MIX_LIMITS, type AudienceMix, mergeMix, sharePercent } from './audienceMix'

// The audience mix rides the engagement-prefs row's GET/PUT; the server re-validates and clamps
// every field (utilities/ai/audience_mix.py), so this card only bounds the inputs.
export default function AudienceMixCard() {
  const { eng, setEng } = useEngagementPrefs()
  if (!eng) return null
  const mix = (eng.audience_mix ?? {}) as AudienceMix
  const update = (patch: Partial<AudienceMix>) => setEng({ audience_mix: mergeMix(mix, patch) })
  const percent = sharePercent(mix)

  return (
    <SectionCard
      title="Who my posts are for"
      blurb="Serve two audiences: each post is written for ONE of them, alternating at the share you set. Leave the share at 0% to write every post for your main audience."
    >
      <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
        <Field settingKey="audience_primary">
          <textarea value={mix.primary_audience ?? ''} rows={2}
            maxLength={AUDIENCE_MIX_LIMITS.description}
            onChange={(e) => update({ primary_audience: e.target.value })}
            placeholder="e.g. ops and engineering leaders already running AI"
            className={inputClass} />
        </Field>
        <Field settingKey="audience_secondary">
          <textarea value={mix.secondary_audience ?? ''} rows={2}
            maxLength={AUDIENCE_MIX_LIMITS.description}
            onChange={(e) => update({ secondary_audience: e.target.value })}
            placeholder="e.g. small-business owners deciding whether AI is worth it"
            className={inputClass} />
        </Field>
      </div>
      <Field settingKey="audience_secondary_share">
        <div className="flex items-center gap-3">
          <input type="range" min={0} max={100} step={10} data-testid="audience-share"
            aria-label="Share of posts for the second audience" value={percent}
            onChange={(e) => update({ secondary_share: Number(e.target.value) / 100 })}
            className="w-48 accent-gray-700" />
          <span className="text-sm text-gray-700 tabular-nums">
            {percent}% of posts for the second audience
          </span>
        </div>
      </Field>
      <Field settingKey="audience_secondary_topics">
        <CsvInput value={mix.secondary_focus_topics ?? []}
          onChange={(values) => update({
            secondary_focus_topics: values.slice(0, AUDIENCE_MIX_LIMITS.topics),
          })}
          placeholder="e.g. cutting busywork, customer response time, cash flow" />
      </Field>
    </SectionCard>
  )
}
