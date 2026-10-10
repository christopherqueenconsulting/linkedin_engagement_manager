import { useEffect, useState } from 'react'
import { DAILY_AI_LIMIT_EVENT } from '../utils/dailyAiLimit'

// The ONE surface for the free-trial daily AI limit (issue #2378). The api client announces every
// 429 the cap answers, so a page that never heard of the limit still tells the user why nothing
// was generated instead of showing only its generic error.
export default function DailyAiLimitNotice() {
  const [message, setMessage] = useState<string | null>(null)

  useEffect(() => {
    const onLimit = (event: Event) => {
      const detail = (event as CustomEvent<string>).detail
      setMessage(typeof detail === 'string' ? detail : null)
    }
    window.addEventListener(DAILY_AI_LIMIT_EVENT, onLimit)
    return () => window.removeEventListener(DAILY_AI_LIMIT_EVENT, onLimit)
  }, [])

  if (!message) return null

  return (
    <div
      role="alert"
      className="fixed top-4 left-1/2 -translate-x-1/2 z-50 max-w-md w-[calc(100%-2rem)]
                 bg-amber-50 border border-amber-300 rounded-lg shadow-lg p-4"
    >
      <p className="text-sm font-semibold text-amber-900">Daily AI limit reached</p>
      <p className="mt-1 text-sm text-amber-800">{message}</p>
      <button
        type="button"
        onClick={() => setMessage(null)}
        className="mt-3 px-3 py-1.5 text-sm font-medium rounded-md bg-amber-600 text-white hover:bg-amber-700"
      >
        Dismiss
      </button>
    </div>
  )
}
