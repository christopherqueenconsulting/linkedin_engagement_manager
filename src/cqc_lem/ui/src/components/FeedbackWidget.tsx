import { useState } from 'react'
import { useLocation } from 'react-router-dom'
import { useAuth } from '../contexts/AuthContext'
import { useAppInfo } from '../hooks/useAppInfo'
import api from '../api/client'
import {
  EVENTS, analyticsSessionId, capture, ensureSessionRecorded, replayEnabled,
} from '../utils/analytics'

const TYPE_HINTS = [
  { value: 'bug', label: '🐞 Something is broken' },
  { value: 'idea', label: '💡 Idea / feature request' },
  { value: 'confusing', label: '🤔 Confusing or unclear' },
  { value: 'praise', label: '🎉 This worked well' },
  { value: 'other', label: '💬 Something else' },
]

// Base64 inflates a file by ~4/3, and the API caps the encoded data URL at 2,000,000 chars.
const MAX_SCREENSHOT_BYTES = 1_400_000

function readAsDataUrl(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader()
    reader.onload = () => resolve(String(reader.result))
    reader.onerror = () => reject(new Error('Could not read the image'))
    reader.readAsDataURL(file)
  })
}

export default function FeedbackWidget() {
  const { user, sessionToken } = useAuth()
  const { data: appInfo } = useAppInfo()
  const location = useLocation()

  const [isOpen, setIsOpen] = useState(false)
  const [typeHint, setTypeHint] = useState('bug')
  const [body, setBody] = useState('')
  const [screenshot, setScreenshot] = useState<string | null>(null)
  const [screenshotName, setScreenshotName] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [sent, setSent] = useState(false)

  function close() {
    setIsOpen(false)
    setError(null)
    if (sent) {
      // Reset only after a successful send so a failed attempt keeps what was typed.
      setSent(false)
      setBody('')
      setScreenshot(null)
      setScreenshotName(null)
      setTypeHint('bug')
    }
  }

  async function handleScreenshot(e: React.ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0]
    if (!file) return
    if (file.size > MAX_SCREENSHOT_BYTES) {
      setError('That image is too large — please attach one under 1.4 MB.')
      e.target.value = ''
      return
    }
    try {
      setScreenshot(await readAsDataUrl(file))
      setScreenshotName(file.name)
      setError(null)
    } catch {
      setError('Could not read that image.')
    }
  }

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault()
    setError(null)
    setLoading(true)
    try {
      await api.post('/feedback', {
        body: body.trim(),
        session_token: sessionToken,
        source: 'widget',
        type_hint: typeHint,
        screenshot,
        context: {
          route: `${location.pathname}${location.search}`,
          user_id: user?.userId ?? null,
          app_version: appInfo?.version ?? null,
          posthog_session_id: analyticsSessionId() ?? null,
          user_agent: navigator.userAgent,
          viewport: `${window.innerWidth}x${window.innerHeight}`,
        },
      })
      setSent(true)
    } catch (err: unknown) {
      const msg = (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail
      setError(typeof msg === 'string' ? msg : 'Could not send your feedback. Please try again.')
    } finally {
      setLoading(false)
    }
  }

  if (!isOpen) {
    return (
      // Someone opening this is about to describe a bug, so their session is worth the quota —
      // ensureSessionRecorded() records it even if sampling left it out, or the replay link on the
      // filed issue would 404 for the ~90% of sessions the sample excludes.
      <button
        onClick={() => {
          setIsOpen(true)
          ensureSessionRecorded()
          capture(EVENTS.feedbackOpened, { route: location.pathname })
        }}
        className="bg-blue-600 text-white text-xs font-semibold px-3.5 py-2 rounded-full shadow-lg hover:bg-blue-700 transition-colors"
        aria-label="Feedback / Report a bug"
      >
        Feedback / Report a bug
      </button>
    )
  }

  return (
    // Positioned by FloatingDock (issue #596); `relative` keeps the close button anchored to the
    // panel now that the panel is no longer the fixed element.
    // The width clamp is not the whole story on a phone: this form is ~350px tall and the dock
    // grows it UPWARDS off the top of a landscape screen (worse with the keyboard open, which
    // autoFocus guarantees), so it needs the same height cap the modals wear (issue #894).
    <div className="relative w-[22rem] max-w-[calc(100vw-2rem)] max-h-viewport overflow-y-auto bg-white border border-gray-200 rounded-xl shadow-xl p-4">
      <button
        onClick={close}
        className="absolute top-2 right-3 text-gray-400 hover:text-gray-600 text-xl leading-none"
        aria-label="Close feedback"
      >
        ×
      </button>

      {sent ? (
        <div className="pt-2">
          <h2 className="text-sm font-bold text-gray-800 mb-1">Thanks — got it 🙏</h2>
          <p className="text-xs text-gray-500 mb-4">
            Your notes go straight to the team along with the page you were on.
          </p>
          <button
            onClick={close}
            className="w-full bg-blue-600 text-white py-2 rounded-lg text-xs font-semibold hover:bg-blue-700 transition-colors"
          >
            Done
          </button>
        </div>
      ) : (
        <form onSubmit={handleSubmit} className="space-y-3 pt-1">
          <h2 className="text-sm font-bold text-gray-800">Feedback / Report a bug</h2>
          <select
            value={typeHint}
            onChange={(e) => setTypeHint(e.target.value)}
            aria-label="Feedback type"
            className="w-full border border-gray-300 rounded-lg px-2 py-1.5 text-xs focus:outline-none focus:ring-2 focus:ring-blue-500"
          >
            {TYPE_HINTS.map((t) => (
              <option key={t.value} value={t.value}>{t.label}</option>
            ))}
          </select>
          <textarea
            required
            autoFocus
            rows={4}
            maxLength={5000}
            value={body}
            onChange={(e) => setBody(e.target.value)}
            aria-label="What happened?"
            placeholder="What happened, or what would you change?"
            className="w-full border border-gray-300 rounded-lg px-3 py-2 text-xs focus:outline-none focus:ring-2 focus:ring-blue-500"
          />
          <label className="block text-[11px] text-gray-500">
            Screenshot (optional)
            <input
              type="file"
              accept="image/*"
              onChange={handleScreenshot}
              className="mt-1 block w-full text-[11px] text-gray-500 file:mr-2 file:py-1 file:px-2 file:rounded file:border-0 file:bg-gray-100 file:text-gray-600"
            />
          </label>
          {screenshotName && (
            <p className="text-[11px] text-gray-500">Attached: {screenshotName}</p>
          )}
          <p className="text-[11px] text-gray-400">
            We attach the page you're on{appInfo?.version ? ` and app v${appInfo.version}` : ''} automatically.
            {/* Say it plainly — opening this panel starts a recording the report will link to. */}
            {replayEnabled() && ' We also record this session and link it to your report (typed text is masked).'}
          </p>
          {error && <p className="text-xs text-red-600">{error}</p>}
          <button
            type="submit"
            disabled={loading || !body.trim()}
            className="w-full bg-blue-600 text-white py-2 rounded-lg text-xs font-semibold hover:bg-blue-700 disabled:opacity-50 transition-colors"
          >
            {loading ? 'Sending…' : 'Send feedback'}
          </button>
        </form>
      )}
    </div>
  )
}
