import { useUserPrefs } from './userPrefsCtx'
import { Field, SectionCard, inputClass } from './Field'
import { INACTIVATE_OPTIONS } from './options'

// The inactivity auto-stop overrides every other setting in this hub, so it lives in Setup with a
// conflict informer (C17) rather than at the bottom of a "Preferences" card.
export default function SafetyCard() {
  const { prefs, setPrefs } = useUserPrefs()

  return (
    <SectionCard title="Safety" blurb="What happens if you stop using LEM.">
      <Field settingKey="last_login_inactivate_delay">
        <select
          value={prefs?.last_login_inactivate_delay === null || prefs === null
            ? 'never'
            : String(prefs.last_login_inactivate_delay)}
          disabled={!prefs}
          onChange={(e) => setPrefs({
            last_login_inactivate_delay: e.target.value === 'never' ? null : Number(e.target.value),
          })}
          className={inputClass}
        >
          {INACTIVATE_OPTIONS.map(({ label, value }) => (
            <option key={String(value)} value={value === null ? 'never' : String(value)}>{label}</option>
          ))}
        </select>
      </Field>
    </SectionCard>
  )
}
