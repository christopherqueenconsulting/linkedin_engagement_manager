import Toggle from '../../../components/Toggle'
import { useEngagementPrefs } from './EngagementPrefsContext'
import { useUserPrefs } from './UserPrefsContext'
import { Advanced, Field, SectionCard, inputClass } from './Field'

// Everything about producing and shipping a post. The content-buffer knobs (F4) and the review
// thresholds are exposed here under Advanced — the buffer settings drive real AI spend and had no
// UI at all despite being read by run_content_plan.py.
export default function ContentSection() {
  const { eng, setEng } = useEngagementPrefs()
  const { prefs, setPrefs } = useUserPrefs()
  if (!eng) return null

  const gateDefaults = eng.gate_defaults ?? { authenticity_score_min: 60, post_similarity_max_pct: 55 }

  return (
    <SectionCard title="Publishing" blurb="What LEM generates, and what has to clear review before it ships.">
      <Field settingKey="auto_schedule_posts">
        <Toggle on={!!prefs?.auto_schedule_posts}
          onClick={() => prefs && setPrefs({ auto_schedule_posts: !prefs.auto_schedule_posts })} />
      </Field>
      <Field settingKey="link_in_first_comment">
        <Toggle on={eng.link_in_first_comment}
          onClick={() => setEng({ link_in_first_comment: !eng.link_in_first_comment })} />
      </Field>
      <Field settingKey="default_video_quality">
        <select value={eng.default_video_quality ?? 'standard'}
          onChange={(e) => setEng({ default_video_quality: e.target.value })} className={inputClass}>
          <option value="standard">Standard (free)</option>
          <option value="premium">Premium (uses 1 video credit)</option>
          <option value="premium_top">Premium Top (uses 3 video credits)</option>
        </select>
      </Field>

      <Advanced>
        <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
          <Field settingKey="authenticity_score_min">
            <input type="number" min={0} max={100} value={eng.authenticity_score_min ?? ''}
              placeholder={`Default ${gateDefaults.authenticity_score_min}`}
              onChange={(e) => setEng({
                authenticity_score_min: e.target.value === '' ? null : Number(e.target.value),
              })}
              className={inputClass} />
          </Field>
          <Field settingKey="post_similarity_max_pct">
            <input type="number" min={10} max={100} value={eng.post_similarity_max_pct ?? ''}
              placeholder={`Default ${gateDefaults.post_similarity_max_pct}%`}
              onChange={(e) => setEng({
                post_similarity_max_pct: e.target.value === '' ? null : Number(e.target.value),
              })}
              className={inputClass} />
          </Field>
          <Field settingKey="content_buffer_days">
            <input type="number" min={1} max={30} value={prefs?.content_buffer_days ?? ''} disabled={!prefs}
              onChange={(e) => setPrefs({ content_buffer_days: Number(e.target.value) })}
              className={inputClass} />
          </Field>
          <Field settingKey="content_buffer_max_posts">
            <input type="number" min={1} max={30} value={prefs?.content_buffer_max_posts ?? ''} disabled={!prefs}
              onChange={(e) => setPrefs({ content_buffer_max_posts: Number(e.target.value) })}
              className={inputClass} />
          </Field>
        </div>
      </Advanced>
    </SectionCard>
  )
}
