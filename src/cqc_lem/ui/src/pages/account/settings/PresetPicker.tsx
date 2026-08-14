import { useEngagementPrefs } from './engagementPrefsCtx'
import { PRESETS, detectPreset, presetValues } from './presets'

// Guided presets (issue #558 §7). Picking one sets a coherent combination of caps + approval
// posture; changing any single value it owns flips the selector back to "Custom". Existing users
// are never migrated onto a preset — they simply show Custom until they choose one.
export default function PresetPicker() {
  const { eng, setEng, catchupAllowed, presetApplied } = useEngagementPrefs()
  if (!eng) return null
  const active = detectPreset(eng, catchupAllowed)

  return (
    <div className="bg-white rounded-lg shadow-sm border border-gray-200 p-6 space-y-4">
      <div>
        <h2 className="text-base font-semibold text-gray-700">Activity level</h2>
        <p className="text-xs text-gray-500 mt-0.5">
          These are the same ramp we run our own account on. Pick one as a starting point, then tune
          anything below — the selector switches to Custom as soon as you do.
        </p>
      </div>
      {presetApplied && (
        <p className="text-xs text-blue-700 bg-blue-50 border border-blue-100 rounded-lg px-3 py-2" data-testid="preset-applied">
          New account — we've started you on <span className="font-semibold">Balanced</span>. Nothing
          is stored until you save, so change anything you like first.
        </p>
      )}
      <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
        {PRESETS.map((p) => {
          const values = presetValues(p.key, catchupAllowed)
          const selected = active === p.key
          return (
            <button
              key={p.key}
              type="button"
              data-testid={`preset-${p.key}`}
              aria-pressed={selected}
              onClick={() => setEng(values)}
              className={`text-left rounded-xl border p-4 transition-colors ${
                selected ? 'border-blue-500 bg-blue-50' : 'border-gray-200 hover:border-gray-300'
              }`}
            >
              <p className="text-sm font-semibold text-gray-800">
                {p.label}
                {p.key === 'balanced' && <span className="ml-1 text-xs font-normal text-blue-600">recommended</span>}
              </p>
              <p className="text-xs text-gray-500 mt-1">{p.blurb}</p>
              <p className="text-xs text-gray-400 mt-2">
                {values.max_comments_per_day} comments · {values.max_dms_per_day} DMs ·{' '}
                {values.max_invites_per_day} invites / day
              </p>
            </button>
          )
        })}
      </div>
      <p className="text-xs text-gray-500" data-testid="preset-state">
        Current: <span className="font-semibold">{active ? PRESETS.find((p) => p.key === active)!.label : 'Custom'}</span>
      </p>
    </div>
  )
}
