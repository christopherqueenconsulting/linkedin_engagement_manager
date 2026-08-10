import { useQuery } from '@tanstack/react-query'
import Toggle from '../../../components/Toggle'
import api from '../../../api/client'
import { useAuth } from '../../../contexts/AuthContext'
import { useEngagementPrefs } from './EngagementPrefsContext'
import { useUserPrefs } from './UserPrefsContext'
import { Advanced, Field, SectionCard, inputClass } from './Field'
import { CADENCE_OPTIONS, DEFAULT_POSTING_DAYS, WEEKDAY_OPTIONS, weekdayLabels, weeklyPostSlots } from './options'

// Everything about producing and shipping a post. The content-buffer knobs (F4) and the review
// thresholds are exposed here under Advanced — the buffer settings drive real AI spend and had no
// UI at all despite being read by run_content_plan.py.
export default function ContentSection() {
  const { eng, setEng } = useEngagementPrefs()
  const { prefs, setPrefs } = useUserPrefs()
  if (!eng) return null

  const gateDefaults = eng.gate_defaults ?? { authenticity_score_min: 60, post_similarity_max_pct: 55 }

  const postingDays = eng.posting_days?.length ? eng.posting_days : DEFAULT_POSTING_DAYS
  // Never let the last day be switched off — an empty set is normalised straight back to Mon-Fri
  // server-side, so the UI would silently discard the click.
  const toggleDay = (day: number) => {
    const next = postingDays.includes(day)
      ? postingDays.filter((d) => d !== day)
      : [...postingDays, day].sort((a, b) => a - b)
    if (next.length) setEng({ posting_days: next })
  }
  const resolvedDays = weeklyPostSlots(eng.posts_per_week ?? 3, postingDays)

  return (
    <SectionCard title="Publishing" blurb="What LEM generates, and what has to clear review before it ships.">
      <Field settingKey="posts_per_week">
        <select value={eng.posts_per_week ?? 3}
          onChange={(e) => setEng({ posts_per_week: Number(e.target.value) })} className={inputClass}>
          {CADENCE_OPTIONS.map((o) => (
            <option key={o.value} value={o.value}>{o.label}</option>
          ))}
        </select>
      </Field>
      <Field settingKey="posting_days">
        <div className="flex flex-wrap gap-2">
          {WEEKDAY_OPTIONS.map((o) => (
            <button key={o.value} type="button" onClick={() => toggleDay(o.value)}
              aria-pressed={postingDays.includes(o.value)}
              className={`px-3 py-1 rounded-full border text-sm ${postingDays.includes(o.value)
                ? 'bg-blue-600 border-blue-600 text-white'
                : 'bg-white border-gray-300 text-gray-600'}`}>
              {o.label}
            </button>
          ))}
        </div>
        <p className="mt-2 text-xs text-gray-500">Publishes on: {weekdayLabels(resolvedDays)}</p>
      </Field>
      <Field settingKey="auto_schedule_posts">
        <Toggle on={!!prefs?.auto_schedule_posts}
          onClick={() => prefs && setPrefs({ auto_schedule_posts: !prefs.auto_schedule_posts })} />
      </Field>
      <Field settingKey="link_in_first_comment">
        <Toggle on={eng.link_in_first_comment}
          onClick={() => setEng({ link_in_first_comment: !eng.link_in_first_comment })} />
      </Field>
      <Field settingKey="text_post_images">
        <Toggle on={eng.text_post_images ?? true}
          onClick={() => setEng({ text_post_images: !(eng.text_post_images ?? true) })} />
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
      <ProfileSkillsPanel />
    </SectionCard>
  )
}

function ProfileSkillsPanel() {
  const { sessionToken } = useAuth()
  const { eng, setEng, save } = useEngagementPrefs()
  const { data, isLoading } = useQuery({
    queryKey: ['linkedin-profile-skills', sessionToken],
    queryFn: () =>
      api
        .get(`/user/linkedin-profile-skills?session_token=${encodeURIComponent(sessionToken!)}`)
        .then((r) => r.data.detail as { skills: string[]; adopted: string[]; focus_topics: string[] }),
    enabled: !!sessionToken,
    staleTime: 5 * 60 * 1000,
  })
  if (!eng || isLoading || !data || data.skills.length === 0) return null

  const unadopted = data.skills.filter((s) => !data.adopted.includes(s))
  const adopt = async () => {
    const merged = Array.from(new Set([...(eng.focus_topics || []), ...data.skills]))
    setEng({ focus_topics: merged })
    // Defer the save so the mutation sees the updated state.
    setTimeout(() => save(), 0)
  }

  return (
    <div className="mt-4 rounded-lg border border-blue-100 bg-blue-50 px-4 py-3">
      <h4 className="text-sm font-semibold text-blue-900">Profile skills</h4>
      <p className="text-xs text-blue-800 mt-1">
        Top skills from your LinkedIn profile:
        {' '}
        {data.skills.map((s) => (
          <span
            key={s}
            className={`inline-block mr-1 mb-1 px-2 py-0.5 rounded-full text-xs ${
              data.adopted.includes(s) ? 'bg-green-100 text-green-800' : 'bg-white text-blue-800 border border-blue-200'
            }`}
          >
            {s}
          </span>
        ))}
      </p>
      {unadopted.length > 0 && (
        <button
          type="button"
          onClick={adopt}
          className="mt-2 text-xs font-semibold text-white bg-blue-600 hover:bg-blue-700 px-3 py-1.5 rounded-md"
        >
          Adopt {unadopted.length} skill{unadopted.length === 1 ? '' : 's'} as focus topics
        </button>
      )}
      {unadopted.length === 0 && (
        <p className="mt-2 text-xs text-green-700">All top profile skills already match your focus topics.</p>
      )}
    </div>
  )
}
