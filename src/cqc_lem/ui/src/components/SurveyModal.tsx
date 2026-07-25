import { useState } from 'react'
import { useAuth } from '../contexts/AuthContext'
import { useSurvey } from '../hooks/useSurvey'
import api from '../api/client'

// NPS/CSAT + review capture (issue #501). The server decides WHICH survey is due (day-3 NPS,
// trial T-3d NPS, or the review that unlocks the extended trial) and asks each one only once;
// this component just renders it. Promoters (9-10) are handed the review form straight after.
export default function SurveyModal() {
  const { sessionToken } = useAuth()
  const { data, refetch } = useSurvey()

  const [closed, setClosed] = useState(false)
  const [showReview, setShowReview] = useState(false)
  const [score, setScore] = useState<number | null>(null)
  const [why, setWhy] = useState('')
  const [rating, setRating] = useState<number | null>(null)
  const [improvement, setImprovement] = useState('')
  const [testimonial, setTestimonial] = useState('')
  const [consent, setConsent] = useState(false)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [done, setDone] = useState<string | null>(null)

  const survey = data?.survey
  if (!survey || closed) return null

  const isReview = survey.source === 'review' || showReview
  const maxScore = data?.nps_max ?? 10
  const maxRating = data?.review_max_rating ?? 5

  function failed(err: unknown, fallback: string) {
    const msg = (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail
    setError(typeof msg === 'string' ? msg : fallback)
  }

  // Always the key of the survey the SERVER asked for — never the promoter chain-on review, which
  // has no ledger key of its own. Closing that opportunistic upsell must not burn the scheduled
  // review_trial_end ask, or the user loses their shot at the extended trial (#499).
  async function dismiss() {
    setClosed(true)
    try {
      await api.post('/survey/dismiss', { session_token: sessionToken, survey_key: survey!.key })
    } catch {
      // Dismissal is best-effort — the modal is already gone for this session either way.
    }
    refetch()
  }

  async function submitNps() {
    if (score === null) return
    setLoading(true)
    setError(null)
    try {
      const resp = await api.post('/survey/nps', {
        session_token: sessionToken,
        score,
        why: why.trim() || null,
        survey_key: survey!.key,
      })
      if (resp.data.detail?.review_invite) {
        setShowReview(true)
      } else {
        setDone('Thanks — that goes straight to the team.')
      }
      refetch()
    } catch (err: unknown) {
      failed(err, 'Could not save your score. Please try again.')
    } finally {
      setLoading(false)
    }
  }

  async function submitReview() {
    if (rating === null) return
    setLoading(true)
    setError(null)
    try {
      await api.post('/survey/review', {
        session_token: sessionToken,
        rating,
        improvement: improvement.trim() || null,
        testimonial: testimonial.trim() || null,
        consent_testimonial: consent,
        // Only the standalone review survey closes a ledger ask; the promoter chain-on already
        // closed its NPS key when the score was submitted.
        survey_key: survey!.source === 'review' ? survey!.key : null,
      })
      setDone('Thanks — your review is in, and it unlocks the extended trial.')
      refetch()
    } catch (err: unknown) {
      failed(err, 'Could not save your review. Please try again.')
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-end sm:items-center justify-center bg-black/40 p-4">
      <div className="relative w-full max-w-md bg-white border border-gray-200 rounded-xl shadow-xl p-5">
        <button
          onClick={dismiss}
          className="absolute top-2 right-3 text-gray-400 hover:text-gray-600 text-xl leading-none"
          aria-label="Close survey"
        >
          ×
        </button>

        {done ? (
          <div className="pt-2">
            <h2 className="text-sm font-bold text-gray-800 mb-1">Thanks 🙏</h2>
            <p className="text-xs text-gray-500 mb-4">{done}</p>
            <button
              onClick={() => setClosed(true)}
              className="w-full bg-blue-600 text-white py-2 rounded-lg text-xs font-semibold hover:bg-blue-700 transition-colors"
            >
              Done
            </button>
          </div>
        ) : isReview ? (
          <div className="space-y-3 pt-1">
            <h2 className="text-sm font-bold text-gray-800">
              {showReview ? 'Turn that into a review?' : survey.headline}
            </h2>
            <p className="text-xs text-gray-500">
              {showReview
                ? 'A short review unlocks the early-adopter extended trial.'
                : survey.body}
            </p>

            <div className="flex gap-1" role="group" aria-label="Rating">
              {Array.from({ length: maxRating }, (_, i) => i + 1).map((n) => (
                <button
                  key={n}
                  type="button"
                  onClick={() => setRating(n)}
                  aria-label={`${n} star${n > 1 ? 's' : ''}`}
                  aria-pressed={rating === n}
                  className={`w-9 h-9 rounded-lg border text-sm ${
                    rating !== null && n <= rating
                      ? 'bg-yellow-400 border-yellow-400 text-white'
                      : 'bg-white border-gray-300 text-gray-400 hover:border-blue-400'
                  }`}
                >
                  ★
                </button>
              ))}
            </div>

            <textarea
              rows={3}
              maxLength={5000}
              value={improvement}
              onChange={(e) => setImprovement(e.target.value)}
              aria-label="What would make this a 10?"
              placeholder="What would make this a 10?"
              className="w-full border border-gray-300 rounded-lg px-3 py-2 text-xs focus:outline-none focus:ring-2 focus:ring-blue-500"
            />
            <textarea
              rows={3}
              maxLength={5000}
              value={testimonial}
              onChange={(e) => setTestimonial(e.target.value)}
              aria-label="Your review"
              placeholder="Your review, in your words (optional)"
              className="w-full border border-gray-300 rounded-lg px-3 py-2 text-xs focus:outline-none focus:ring-2 focus:ring-blue-500"
            />
            <label className="flex items-start gap-2 text-[11px] text-gray-600">
              <input
                type="checkbox"
                checked={consent}
                onChange={(e) => setConsent(e.target.checked)}
                className="mt-0.5"
              />
              You may quote this (with my name) as a testimonial.
            </label>

            {error && <p className="text-xs text-red-600">{error}</p>}
            <button
              type="button"
              onClick={submitReview}
              disabled={loading || rating === null}
              className="w-full bg-blue-600 text-white py-2 rounded-lg text-xs font-semibold hover:bg-blue-700 disabled:opacity-50 transition-colors"
            >
              {loading ? 'Sending…' : 'Submit review'}
            </button>
          </div>
        ) : (
          <div className="space-y-3 pt-1">
            <h2 className="text-sm font-bold text-gray-800">{survey.headline}</h2>
            <p className="text-xs text-gray-500">{survey.body}</p>

            <div className="flex flex-wrap gap-1" role="group" aria-label="Score 0 to 10">
              {Array.from({ length: maxScore + 1 }, (_, n) => n).map((n) => (
                <button
                  key={n}
                  type="button"
                  onClick={() => setScore(n)}
                  aria-label={`Score ${n}`}
                  aria-pressed={score === n}
                  className={`w-8 h-8 rounded-lg border text-xs font-semibold ${
                    score === n
                      ? 'bg-blue-600 border-blue-600 text-white'
                      : 'bg-white border-gray-300 text-gray-600 hover:border-blue-400'
                  }`}
                >
                  {n}
                </button>
              ))}
            </div>
            <div className="flex justify-between text-[11px] text-gray-400">
              <span>Not likely</span>
              <span>Very likely</span>
            </div>

            <textarea
              rows={3}
              maxLength={5000}
              value={why}
              onChange={(e) => setWhy(e.target.value)}
              aria-label="Why that score?"
              placeholder="What's the main reason for your score?"
              className="w-full border border-gray-300 rounded-lg px-3 py-2 text-xs focus:outline-none focus:ring-2 focus:ring-blue-500"
            />

            {error && <p className="text-xs text-red-600">{error}</p>}
            <button
              type="button"
              onClick={submitNps}
              disabled={loading || score === null}
              className="w-full bg-blue-600 text-white py-2 rounded-lg text-xs font-semibold hover:bg-blue-700 disabled:opacity-50 transition-colors"
            >
              {loading ? 'Sending…' : survey.cta_label}
            </button>
          </div>
        )}
      </div>
    </div>
  )
}
