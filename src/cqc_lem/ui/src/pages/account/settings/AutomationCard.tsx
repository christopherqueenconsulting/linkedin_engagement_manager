import { useState } from 'react'
import { useEngagementPrefs } from './engagementPrefsCtx'
import { SectionCard } from './Field'
import { AUTOMATION_LANES, type ModeKey } from './automationLanes'

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
