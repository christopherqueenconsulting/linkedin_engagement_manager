import { useState } from 'react'
import { Link } from 'react-router-dom'
import { useAuth } from '../contexts/AuthContext'

const DISMISS_KEY = 'lem_strong_factor_prompt_dismissed'

function deadlineLabel(iso: string | null): string | null {
  if (!iso) return null
  const d = new Date(iso)
  return Number.isNaN(d.getTime()) ? null : d.toLocaleDateString()
}

/**
 * The pre-deadline nudge (issue #905, design §7 Stage 2).
 *
 * Mandatory enrolment is a date, and a date nobody was warned about is a support ticket. This is
 * the warning: it appears the moment `REQUIRE_STRONG_FACTOR_AFTER` is scheduled, says when, and
 * links to the card that makes it go away.
 *
 * It is dismissible, and dismissal is deliberately BROWSER state — enrolling is what ends the
 * prompt for good (the server stops sending `strong_factor_prompt` the moment a factor exists), so
 * a dismissal that outlived the deadline could not hide the gate that follows it.
 */
export default function StrongFactorPrompt() {
  const { strongFactorPrompt, strongFactorDeadline, enrollmentRequired } = useAuth()
  const [dismissed, setDismissed] = useState(() => localStorage.getItem(DISMISS_KEY) === '1')

  // The gate supersedes the nudge: once the session is held there is nothing left to warn about.
  if (!strongFactorPrompt || enrollmentRequired || dismissed) return null

  const when = deadlineLabel(strongFactorDeadline)
  const overdue = !!strongFactorDeadline && new Date(strongFactorDeadline) <= new Date()

  return (
    <div
      role="status"
      data-testid="strong-factor-prompt"
      className="mb-4 border border-amber-300 bg-amber-50 rounded-lg px-4 py-3 flex flex-col sm:flex-row sm:items-center gap-2"
    >
      <p className="text-sm text-amber-900 flex-1">
        <span className="font-semibold">Add two-factor sign-in.</span>{' '}
        {overdue
          ? 'From your next sign-in, an emailed code alone will no longer get you in — you will be asked to set this up first.'
          : `From ${when ?? 'soon'}, an emailed code alone will no longer sign you in. Set up a passkey or an authenticator app now so it takes a minute instead of blocking you.`}
      </p>
      <div className="flex items-center gap-3 shrink-0">
        <Link
          to="/account"
          className="bg-amber-600 text-white px-3 py-1.5 rounded-lg text-xs font-semibold hover:bg-amber-700 whitespace-nowrap"
        >
          Set it up
        </Link>
        <button
          type="button"
          onClick={() => {
            localStorage.setItem(DISMISS_KEY, '1')
            setDismissed(true)
          }}
          className="text-xs font-semibold text-amber-900 hover:underline whitespace-nowrap"
        >
          Not now
        </button>
      </div>
    </div>
  )
}
