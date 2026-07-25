import { useState } from 'react'
import { useAuth } from '../contexts/AuthContext'
import { useShippedNotices } from '../hooks/useShippedNotices'
import api from '../api/client'

// "You asked, we shipped" (issue #502) — the last stage of the feedback loop. Shows the oldest
// un-acknowledged notice for a fix THIS user reported, and asks the micro-CSAT: did it fix it?
// A "not yet" answer becomes a new feedback row, so it re-enters the same loop that built the fix.
export default function ShippedNotice() {
  const { sessionToken } = useAuth()
  const { data, refetch } = useShippedNotices()

  const [dismissed, setDismissed] = useState<number[]>([])
  const [showComment, setShowComment] = useState(false)
  const [comment, setComment] = useState('')
  const [loading, setLoading] = useState(false)
  const [done, setDone] = useState<string | null>(null)

  const notice = data?.notices?.find((n) => !dismissed.includes(n.id))
  if (!notice) return null

  async function ack(resolved: boolean | null, text?: string) {
    setLoading(true)
    try {
      await api.post('/shipped/ack', {
        session_token: sessionToken,
        notice_id: notice!.id,
        resolved,
        comment: text?.trim() || null,
      })
      setDone(
        resolved === false
          ? "Thanks — that goes back to the team as a fresh report."
          : resolved === true
            ? 'Great — thanks for closing the loop.'
            : null,
      )
      if (resolved === null) setDismissed((d) => [...d, notice!.id])
      refetch()
    } catch {
      // Best-effort: the notice simply comes back on the next load.
      setDismissed((d) => [...d, notice!.id])
    } finally {
      setLoading(false)
      setShowComment(false)
    }
  }

  // `**bold**` from the changelog line — rendered as plain text, no markdown parser needed.
  const line = notice.changelog_line.replace(/\*\*/g, '')

  return (
    <div className="mb-4 border border-green-200 bg-green-50 rounded-lg p-3">
      <div className="flex items-start gap-2">
        <span aria-hidden="true">🎉</span>
        <div className="flex-1">
          <h2 className="text-sm font-bold text-gray-800">You asked, we shipped</h2>
          <p className="text-xs text-gray-600 mt-0.5">{line}</p>

          {done ? (
            <p className="text-xs text-green-700 mt-2">{done}</p>
          ) : showComment ? (
            <div className="mt-2 space-y-2">
              <textarea
                rows={2}
                maxLength={5000}
                value={comment}
                onChange={(e) => setComment(e.target.value)}
                aria-label="What's still wrong?"
                placeholder="What's still wrong?"
                className="w-full border border-gray-300 rounded-lg px-3 py-2 text-xs focus:outline-none focus:ring-2 focus:ring-blue-500"
              />
              <button
                type="button"
                disabled={loading}
                onClick={() => ack(false, comment)}
                className="bg-blue-600 text-white px-3 py-1.5 rounded-lg text-xs font-semibold hover:bg-blue-700 disabled:opacity-50 transition-colors"
              >
                {loading ? 'Sending…' : 'Send'}
              </button>
            </div>
          ) : (
            <div className="mt-2 flex items-center gap-2">
              <span className="text-xs text-gray-500">Did this fix it?</span>
              <button
                type="button"
                disabled={loading}
                onClick={() => ack(true)}
                className="border border-green-300 bg-white text-green-700 px-2.5 py-1 rounded-lg text-xs font-semibold hover:border-green-500 disabled:opacity-50 transition-colors"
              >
                Yes
              </button>
              <button
                type="button"
                disabled={loading}
                onClick={() => setShowComment(true)}
                className="border border-gray-300 bg-white text-gray-600 px-2.5 py-1 rounded-lg text-xs font-semibold hover:border-blue-400 disabled:opacity-50 transition-colors"
              >
                Not yet
              </button>
            </div>
          )}
        </div>
        <button
          onClick={() => ack(null)}
          className="text-gray-400 hover:text-gray-600 text-lg leading-none"
          aria-label="Dismiss shipped notice"
        >
          ×
        </button>
      </div>
    </div>
  )
}
