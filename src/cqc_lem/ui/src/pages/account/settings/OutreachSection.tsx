import Toggle from '../../../components/Toggle'
import { CATCHUP_EVENTS } from '../types'
import CsvInput from './CsvInput'
import { useEngagementPrefs } from './engagementPrefsCtx'
import { Advanced, Field, SectionCard, inputClass } from './Field'

// Everything that puts a message in front of a person: who we ask to connect, and what we say when
// something happens in their world.
export default function OutreachSection() {
  const { eng, setEng } = useEngagementPrefs()
  if (!eng) return null
  const selectedEvents = eng.catchup_event_types ?? []

  return (
    <>
      <SectionCard title="Connection requests" blurb="Who LEM asks to connect with, and whether you sign off first.">
        <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
          <Field settingKey="connection_targeting_mode">
            <select value={eng.connection_targeting_mode ?? 'suggest'}
              onChange={(e) => setEng({ connection_targeting_mode: e.target.value })} className={inputClass}>
              <option value="off">Off (no targets sourced)</option>
              <option value="suggest">Suggest (drafts for your approval)</option>
              <option value="auto_queue">Auto-queue (use the approval setting)</option>
            </select>
          </Field>
          <Field settingKey="connection_request_mode">
            <select value={eng.connection_request_mode ?? 'auto_approve'}
              onChange={(e) => setEng({ connection_request_mode: e.target.value })} className={inputClass}>
              <option value="auto_approve">Auto-approve (queue targets immediately)</option>
              <option value="pre_review">Pre-review (approve each target first)</option>
            </select>
          </Field>
        </div>
        <Field settingKey="connection_target_authors">
          <CsvInput value={eng.connection_target_authors}
            onChange={(values) => setEng({ connection_target_authors: values })}
            placeholder="https://www.linkedin.com/in/their-slug, https://www.linkedin.com/in/another" />
        </Field>
        <Advanced>
          <Field settingKey="min_connection_icp_score">
            <input type="number" min={0} max={100} value={eng.min_connection_icp_score ?? 55}
              onChange={(e) => setEng({ min_connection_icp_score: Number(e.target.value) })}
              className={inputClass} />
          </Field>
        </Advanced>
      </SectionCard>

      <SectionCard title="Profile viewers" blurb="What happens when someone looks at your profile but has not engaged with you.">
        <Field settingKey="profile_viewer_dm_auto_send">
          <Toggle on={!!eng.profile_viewer_dm_auto_send}
            onClick={() => setEng({ profile_viewer_dm_auto_send: !eng.profile_viewer_dm_auto_send })} />
        </Field>
      </SectionCard>

      <SectionCard title="Catch-up congratulations" blurb="Milestones in your network that are worth a message. Their volume is capped under How Much & How Often.">
        <Field settingKey="catchup_event_types">
          <div className="flex flex-wrap gap-3 pt-1">
            {CATCHUP_EVENTS.map((ev) => {
              const selected = selectedEvents.includes(ev.key)
              return (
                <label key={ev.key} className="flex items-center gap-1.5 text-sm text-gray-600">
                  <input type="checkbox" checked={selected}
                    onChange={() => setEng({
                      catchup_event_types: selected
                        ? selectedEvents.filter((k) => k !== ev.key)
                        : [...selectedEvents, ev.key],
                    })} />
                  {ev.label}
                </label>
              )
            })}
          </div>
        </Field>
        <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
          <Field settingKey="catchup_touch_mode">
            <select value={eng.catchup_touch_mode ?? 'pre_review'}
              onChange={(e) => setEng({ catchup_touch_mode: e.target.value })} className={inputClass}>
              <option value="pre_review">Pre-review (edit + approve each message)</option>
              <option value="auto_approve">Auto-use (send LinkedIn's draft as-is)</option>
            </select>
          </Field>
          <Field settingKey="catchup_message_source">
            <select value={eng.catchup_message_source ?? 'linkedin'}
              onChange={(e) => setEng({ catchup_message_source: e.target.value })} className={inputClass}>
              <option value="linkedin">LinkedIn's suggested response (default)</option>
              <option value="ai">Customize with AI (your DM template + voice)</option>
            </select>
          </Field>
        </div>
      </SectionCard>
    </>
  )
}
