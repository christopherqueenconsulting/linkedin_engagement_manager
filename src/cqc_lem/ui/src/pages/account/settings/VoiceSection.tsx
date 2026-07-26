import Toggle from '../../../components/Toggle'
import { csv, parseCsv } from '../types'
import { FIELD_LIMITS } from '../fieldLimits'
import { useEngagementPrefs } from './EngagementPrefsContext'
import { useUserPrefs } from './UserPrefsContext'
import { Field, SectionCard, inputClass } from './Field'
import { CONTENT_LANGUAGE_OPTIONS } from './options'

// Voice governs posts, comments, DMs AND newsletters — it used to sit under "Engagement &
// Automation", where nobody writing content thought to look for it (F10).
export default function VoiceSection() {
  const { eng, setEng } = useEngagementPrefs()
  const { prefs, setPrefs, effectiveLanguage } = useUserPrefs()
  if (!eng) return null

  return (
    <>
      <SectionCard
        title="How I sound"
        blurb="Applies to every comment, DM, post and newsletter LEM writes. Leave blank to infer purely from your profile."
      >
        <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
          <Field settingKey="tone">
            <input type="text" value={eng.tone || ''} maxLength={FIELD_LIMITS.tone}
              onChange={(e) => setEng({ tone: e.target.value })}
              placeholder="e.g. warm, authoritative" className={inputClass} />
          </Field>
          <Field settingKey="comment_length">
            <select value={eng.comment_length} onChange={(e) => setEng({ comment_length: e.target.value })}
              className={inputClass}>
              <option value="short">Short (~30 words)</option>
              <option value="medium">Medium (~55 words · recommended)</option>
              <option value="long">Long (~90 words)</option>
            </select>
          </Field>
        </div>
        <Field settingKey="comment_style">
          <input type="text" value={eng.comment_style || ''} maxLength={FIELD_LIMITS.comment_style}
            onChange={(e) => setEng({ comment_style: e.target.value })}
            placeholder="e.g. ask a question, avoid buzzwords" className={inputClass} />
        </Field>
        <Field settingKey="use_emojis">
          <Toggle on={eng.use_emojis} onClick={() => setEng({ use_emojis: !eng.use_emojis })} />
        </Field>
        <Field settingKey="use_hashtags">
          <Toggle on={eng.use_hashtags} onClick={() => setEng({ use_hashtags: !eng.use_hashtags })} />
        </Field>
        <Field settingKey="content_language">
          <select value={prefs?.content_language ?? ''} disabled={!prefs}
            onChange={(e) => setPrefs({ content_language: e.target.value })}
            className={inputClass}>
            <option value="">
              Auto — from my Login Location{effectiveLanguage ? ` (${effectiveLanguage})` : ''}
            </option>
            {CONTENT_LANGUAGE_OPTIONS.map(({ label, value }) => (
              <option key={value} value={value}>{label}</option>
            ))}
          </select>
        </Field>
      </SectionCard>

      <SectionCard
        title="What I talk about"
        blurb="These steer the ANGLE of your content and keep it aligned to your goals. They never override the subject of the post being engaged with, and LEM never promotes internal tools."
      >
        <Field settingKey="focus_topics">
          <input type="text" value={csv(eng.focus_topics)}
            onChange={(e) => setEng({ focus_topics: parseCsv(e.target.value) })}
            placeholder="e.g. B2B sales, leadership, AI adoption" className={inputClass} />
        </Field>
        <Field settingKey="business_goals">
          <textarea value={eng.business_goals || ''} rows={2} maxLength={FIELD_LIMITS.goals}
            onChange={(e) => setEng({ business_goals: e.target.value })}
            placeholder="e.g. Book 5 discovery calls/month with mid-market ops leaders"
            className={inputClass} />
        </Field>
        <Field settingKey="personal_goals">
          <textarea value={eng.personal_goals || ''} rows={2} maxLength={FIELD_LIMITS.goals}
            onChange={(e) => setEng({ personal_goals: e.target.value })}
            placeholder="e.g. Build a reputation as a thoughtful voice in supply-chain tech"
            className={inputClass} />
        </Field>
      </SectionCard>
    </>
  )
}
