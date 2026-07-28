import { useEffect, useState } from 'react'
import { CHUNK_RELOAD_BLOCKED_EVENT, NEW_VERSION_MESSAGE } from '../utils/chunkReload'

// The visible half of the stale-chunk guard (issue #743). Renders nothing in the normal case: a
// chunk lost to a deploy self-heals with one silent reload. This is only what the user sees when a
// SECOND failure lands inside the reload cooldown — reloading again could loop, so we ask instead.
export default function NewVersionNotice() {
  const [blocked, setBlocked] = useState(false)

  useEffect(() => {
    const onBlocked = () => setBlocked(true)
    window.addEventListener(CHUNK_RELOAD_BLOCKED_EVENT, onBlocked)
    return () => window.removeEventListener(CHUNK_RELOAD_BLOCKED_EVENT, onBlocked)
  }, [])

  if (!blocked) return null

  return (
    <div
      role="alert"
      className="fixed bottom-4 left-1/2 -translate-x-1/2 z-50 max-w-md w-[calc(100%-2rem)]
                 bg-amber-50 border border-amber-300 rounded-lg shadow-lg p-4"
    >
      <p className="text-sm font-semibold text-amber-900">{NEW_VERSION_MESSAGE}</p>
      <p className="mt-1 text-sm text-amber-800">
        This tab has been open since an earlier release, so part of the app could not load.
      </p>
      <button
        type="button"
        onClick={() => window.location.reload()}
        className="mt-3 px-3 py-1.5 text-sm font-medium rounded-md bg-amber-600 text-white hover:bg-amber-700"
      >
        Refresh now
      </button>
    </div>
  )
}
