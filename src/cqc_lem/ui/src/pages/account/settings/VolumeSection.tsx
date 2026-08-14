import { useEngagementPrefs } from './engagementPrefsCtx'
import { Advanced, Field, SectionCard, inputClass } from './Field'
import PresetPicker from './PresetPicker'
import ReplySetupSteps from './ReplySetupSteps'

// The whole volume dial in one place, with the cross-cap interactions made visible instead of
// buried in a line of grey microcopy (F9): catch-up spends the DM budget, and "invites" here means
// connects + profile-viewer invites only.
export default function VolumeSection() {
  const { eng, setEng, catchupAllowed } = useEngagementPrefs()
  if (!eng) return null

  return (
    <>
      <PresetPicker />

      <SectionCard title="Daily caps" blurb="Hard ceilings. When one is reached, that activity stops for the day.">
        <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
          <Field settingKey="max_comments_per_day">
            <input type="number" min={0} value={eng.max_comments_per_day}
              onChange={(e) => setEng({ max_comments_per_day: Number(e.target.value) })}
              className={inputClass} />
          </Field>
          <Field settingKey="max_dms_per_day">
            <input type="number" min={0} value={eng.max_dms_per_day}
              onChange={(e) => setEng({ max_dms_per_day: Number(e.target.value) })}
              className={inputClass} />
          </Field>
          <Field settingKey="max_invites_per_day">
            <input type="number" min={0} value={eng.max_invites_per_day ?? 10}
              onChange={(e) => setEng({ max_invites_per_day: Number(e.target.value) })}
              className={inputClass} />
          </Field>
          <Field settingKey="max_company_page_invites_per_day">
            <input type="number" min={0} max={50} value={eng.max_company_page_invites_per_day ?? 5}
              onChange={(e) => setEng({
                max_company_page_invites_per_day: Math.min(50, Math.max(0, Number(e.target.value) || 0)),
              })}
              className={inputClass} />
          </Field>
          <Field settingKey="max_follows_per_day">
            <input type="number" min={0} max={20} value={eng.max_follows_per_day ?? 3}
              onChange={(e) => setEng({
                max_follows_per_day: Math.min(20, Math.max(0, Number(e.target.value) || 0)),
              })}
              className={inputClass} />
          </Field>
          <Field settingKey="max_catchup_touches_per_day">
            <input type="number" min={0} max={catchupAllowed} value={eng.max_catchup_touches_per_day ?? 5}
              onChange={(e) => setEng({
                max_catchup_touches_per_day: Math.min(catchupAllowed, Number(e.target.value)),
              })}
              className={inputClass} />
          </Field>
        </div>
      </SectionCard>

      <SectionCard title="Replying to comments on my posts" blurb="How LEM notices that somebody replied to you.">
        <Field settingKey="reply_check_mode">
          <select value={eng.reply_check_mode ?? 'event'}
            onChange={(e) => setEng({ reply_check_mode: e.target.value })} className={inputClass}>
            <option value="event">Event-driven — reply when notified (recommended)</option>
            <option value="scheduled">Scheduled — check on a timer</option>
            <option value="off">Off — don't auto-reply</option>
          </select>
        </Field>
        {eng.reply_check_mode === 'event' && <ReplySetupSteps />}
        <Advanced>
          {eng.reply_check_mode === 'scheduled' && (
            <Field settingKey="reply_sweeps_per_day">
              <input type="number" min={2} max={12} value={eng.reply_sweeps_per_day ?? 2}
                onChange={(e) => setEng({
                  reply_sweeps_per_day: Math.min(12, Math.max(2, Number(e.target.value) || 2)),
                })}
                className={inputClass} />
            </Field>
          )}
          <Field settingKey="reply_max_post_age_days">
            <input type="number" min={1} max={14} value={eng.reply_max_post_age_days ?? 2}
              onChange={(e) => setEng({
                reply_max_post_age_days: Math.min(14, Math.max(1, Number(e.target.value) || 2)),
              })}
              className={inputClass} />
          </Field>
        </Advanced>
      </SectionCard>
    </>
  )
}
