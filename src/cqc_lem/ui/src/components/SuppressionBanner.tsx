import { useAutomationStatus, useResumeAutomation } from '../hooks/useAutomationStatus'

// Suppression tripwire (issue #629). LinkedIn never tells an account it has been limited, so when
// LEM detects the reach collapse and stops its own engagement automation, THIS banner is the notice.
// Renders nothing in the healthy case; a 'watch' reading is informational and offers no button,
// because nothing has been stopped yet.
export default function SuppressionBanner() {
  const { data } = useAutomationStatus()
  const resume = useResumeAutomation()
  if (!data) return null

  if (!data.tripped) {
    if (data.current?.status !== 'watch') return null
    return (
      <div className="bg-amber-50 border border-amber-300 rounded-lg p-4">
        <p className="text-sm font-semibold text-amber-900">👀 Watching your reach</p>
        <p className="mt-1 text-sm text-amber-800">{data.current.reason}</p>
        <p className="mt-2 text-xs text-amber-700">
          Nothing has been paused. If the drop holds for several posting days in a row we'll stop
          engagement automation for you and email you.
        </p>
      </div>
    )
  }

  const trippedAt = data.trip?.tripped_at
    ? new Date(data.trip.tripped_at).toLocaleDateString(undefined, {
        year: 'numeric',
        month: 'short',
        day: 'numeric',
      })
    : null

  return (
    <div className="bg-red-50 border border-red-300 rounded-lg p-4">
      <p className="text-sm font-semibold text-red-900">
        ⚠️ Engagement automation is paused — your reach dropped sharply
      </p>
      <p className="mt-1 text-sm text-red-800">
        {data.trip?.reason || data.current?.reason}
        {trippedAt ? ` (detected ${trippedAt})` : ''}
      </p>
      <p className="mt-2 text-sm text-red-800">
        That pattern usually means LinkedIn has quietly limited the account — it never notifies you
        when it does. Your scheduled posts still publish and we keep collecting analytics; only
        automated comments, replies and messages are stopped. Accounts typically recover after a few
        weeks of normal, human activity.
      </p>
      {data.recovered ? (
        <p className="mt-2 text-sm font-medium text-green-800">
          ✓ Your latest readings look healthy again — {data.current?.reason}
        </p>
      ) : (
        <p className="mt-2 text-sm text-red-700">
          Your latest readings still show the drop. Re-enabling now risks extending the limit.
        </p>
      )}
      <button
        type="button"
        onClick={() => resume.mutate()}
        disabled={resume.isPending}
        className="mt-3 px-4 py-2 rounded-md bg-red-600 text-white text-sm font-semibold hover:bg-red-700 disabled:opacity-50"
      >
        {resume.isPending ? 'Re-enabling…' : 'Re-enable engagement automation'}
      </button>
      {resume.isError && (
        <p className="mt-2 text-xs text-red-700">Could not re-enable — please try again.</p>
      )}
      <p className="mt-2 text-xs text-red-600">
        This pause will not lift on its own. You decide when to turn engagement back on.
      </p>
    </div>
  )
}
