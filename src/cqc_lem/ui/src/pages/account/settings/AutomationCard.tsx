import { useState } from 'react'
import { useEngagementPrefs } from './engagementPrefsCtx'
import { SectionCard } from './Field'

type ModeKey = 'nurture_dm_mode' | 'artifact_dm_mode' | 'group_post_mode'

// One row per lane that has a per-user approval mode (issue #2624). `auto` says, in one sentence,
// what switching the lane to auto-approve means.
export const AUTOMATION_LANES: { key: ModeKey; label: string; review: string; auto: string }[] = [
  {
    key: 'nurture_dm_mode',
    label: 'Reply nurture DMs',
    review: 'Each drafted reply to a lead waits for your approval.',
    auto: 'Drafted replies to leads are queued as approved and sent at their slot without you seeing them.',
  },
  {
    key: 'artifact_dm_mode',
    label: 'Lead-magnet DMs',
    review: 'Each resource DM waits for your approval.',
    auto: 'The resource DM goes to anyone who comments your keyword, without you seeing it first.',
  },
  {
    key: 'group_post_mode',
    label: 'Weekly group post',
    review: 'The weekly draft is held until you restore it in the studio.',
    auto: 'The weekly group post publishes at its slot unless you skip it first.',
  },
]

/** Per-lane review vs auto-approve. Turning a lane to auto-approve asks for a named confirmation. */
export default function AutomationCard() {
  const { eng, setEng } = useEngagementPrefs()
  const [pending, setPending] = useState<ModeKey | null>(null)
  if (!eng) return null

  return (
    <SectionCard title="Automation"
      blurb="Every outbound lane starts in review. Switch one to auto-approve only when you trust what it drafts.">
      <div className="space-y-4">
        {AUTOMATION_LANES.map((lane) => {
          const auto = eng[lane.key] === 'auto_approve'
          return (
            <div key={lane.key} data-testid={`automation-${lane.key}`}>
              <div className="flex items-center justify-between gap-3">
                <label className="text-sm font-medium text-fg" htmlFor={lane.key}>{lane.label}</label>
                <select id={lane.key} value={auto ? 'auto_approve' : 'pre_review'}
                  onChange={(e) => {
                    if (e.target.value === 'auto_approve') setPending(lane.key)
                    else { setPending(null); setEng({ [lane.key]: 'pre_review' }) }
                  }}
                  className="border border-hairline rounded-panel px-3 py-2 text-sm">
                  <option value="pre_review">Review first</option>
                  <option value="auto_approve">Auto-approve</option>
                </select>
              </div>
              <p className="text-xs text-fg-muted mt-1">{auto ? lane.auto : lane.review}</p>
              {pending === lane.key && (
                <div role="alertdialog" aria-label={`Confirm auto-approve for ${lane.label}`}
                  className="mt-2 rounded-panel border border-warning/25 bg-warning/5 px-3 py-2 text-xs text-warning">
                  Turn on auto-approve for <strong>{lane.label}</strong>? {lane.auto}
                  <button type="button" className="ml-3 font-semibold underline"
                    onClick={() => { setEng({ [lane.key]: 'auto_approve' }); setPending(null) }}>
                    Yes, auto-approve {lane.label}
                  </button>
                  <button type="button" className="ml-3 font-semibold underline"
                    onClick={() => setPending(null)}>
                    Cancel
                  </button>
                </div>
              )}
            </div>
          )
        })}
      </div>
    </SectionCard>
  )
}
