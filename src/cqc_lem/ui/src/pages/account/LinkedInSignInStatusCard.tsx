import { useQuery } from '@tanstack/react-query'
import api from '../../api/client'
import { useAuth } from '../../contexts/AuthContext'

// The LinkedIn sign-in approval, made visible (issue #933).
//
// When LinkedIn challenges an automated sign-in it asks the account owner to confirm the device
// from the LinkedIn mobile app. That approval happens on LinkedIn, so an email was the only place
// the ask ever appeared and nothing here ever changed afterwards — a user who had already tapped
// "Yes" could not tell whether LEM received it. This card is that confirmation.
//
// `unknown` is NOT a failure: it just means nothing has been recorded (no sign-in since the record
// expired, or the runtime store is unavailable), so it must never read as a broken connection.

type SignInState = 'signed_in' | 'approval_pending' | 'approval_timed_out' | 'unknown'

type SignInStatus = {
  state: SignInState
  signed_in_at: string | null
  approval_requested_at: string | null
  approval_cleared_at: string | null
}

function when(iso: string | null): string | null {
  if (!iso) return null
  const d = new Date(iso)
  return Number.isNaN(d.getTime()) ? null : d.toLocaleString()
}

export default function LinkedInSignInStatusCard() {
  const { sessionToken } = useAuth()

  const { data } = useQuery({
    queryKey: ['linkedin-signin-status', sessionToken],
    queryFn: () =>
      api
        .get(`/user/linkedin-signin-status?session_token=${encodeURIComponent(sessionToken!)}`)
        .then((r) => r.data.detail as SignInStatus),
    enabled: !!sessionToken,
    // While an approval is outstanding the user is staring at this card waiting for it to turn
    // green, so poll; otherwise this is a slow-moving fact and one fetch per visit is plenty.
    refetchInterval: (query) =>
      (query.state.data?.state === 'approval_pending' ? 15_000 : false),
    staleTime: 30 * 1000,
  })

  if (!data) return null

  const lastSignIn = when(data.signed_in_at)
  const askedAt = when(data.approval_requested_at)

  const tone =
    data.state === 'signed_in'
      ? { dot: 'bg-green-500', box: 'border-gray-200' }
      : data.state === 'approval_pending'
        ? { dot: 'bg-amber-500', box: 'border-amber-300' }
        : data.state === 'approval_timed_out'
          ? { dot: 'bg-red-500', box: 'border-red-200' }
          : { dot: 'bg-gray-300', box: 'border-gray-200' }

  const headline =
    data.state === 'signed_in'
      ? 'LinkedIn sign-in confirmed'
      : data.state === 'approval_pending'
        ? 'Waiting for you to approve a LinkedIn sign-in'
        : data.state === 'approval_timed_out'
          ? 'Your LinkedIn sign-in was not approved in time'
          : 'No LinkedIn sign-in recorded yet'

  return (
    <div
      data-testid="linkedin-signin-status"
      className={`bg-white rounded-lg shadow-sm border ${tone.box} p-6 space-y-3`}
    >
      <div className="flex items-center gap-3">
        <div className={`w-3 h-3 rounded-full flex-shrink-0 ${tone.dot}`} />
        <h2 className="text-base font-semibold text-gray-700">LinkedIn Sign-in</h2>
      </div>

      <p className="text-sm text-gray-700">{headline}</p>

      {data.state === 'signed_in' && (
        <p className="text-xs text-gray-500">
          {data.approval_cleared_at
            ? 'Your device approval came through — LEM signed in with it. '
            : ''}
          {lastSignIn
            ? `Last signed in ${lastSignIn}.`
            : 'LEM is signed in to LinkedIn.'}
        </p>
      )}

      {data.state === 'approval_pending' && (
        <div className="text-xs text-amber-900 space-y-1">
          <p>
            LinkedIn asked us to verify this sign-in from your device. Open the{' '}
            <strong>LinkedIn mobile app</strong> and tap <strong>Yes</strong> on the "Did you just
            try to sign in?" prompt — we also emailed you the same steps.
          </p>
          <p>
            Already approved? This turns green on its own within a minute or two{' '}
            {askedAt ? `(asked ${askedAt})` : ''}.
          </p>
        </div>
      )}

      {data.state === 'approval_timed_out' && (
        <div className="text-xs text-gray-600 space-y-1">
          <p>
            We stopped waiting {askedAt ? `after asking ${askedAt}` : ''} — automation will ask
            again on its next run, and approving from the LinkedIn app is all that is needed.
          </p>
          {lastSignIn && <p>Last successful sign-in: {lastSignIn}.</p>}
        </div>
      )}

      {data.state === 'unknown' && (
        <p className="text-xs text-gray-500">
          This fills in the next time automation signs in to LinkedIn. Nothing is wrong — we simply
          have no recent sign-in on record.
        </p>
      )}
    </div>
  )
}
