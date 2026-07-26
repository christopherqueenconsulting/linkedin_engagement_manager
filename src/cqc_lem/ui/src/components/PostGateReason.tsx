import { Link } from 'react-router-dom'
import { gateHold } from '../utils/gateFindings'
import type { GateFinding } from '../utils/gateFindings'

// Why a draft is sitting in PENDING (issue #421). A quality gate demoted it — show WHICH gate, the
// score against the threshold it failed, and the two things the user can actually do: fix the draft
// and re-score it, or loosen the threshold in Account settings.

// Similarity/focus scores are 0-1 overlap fractions; authenticity is already a 0-100 score.
const isFraction = (gate: string) => gate === 'similarity' || gate === 'focus_alignment'
const fmtScore = (gate: string, value: number | null) =>
  value === null || value === undefined
    ? null
    : isFraction(gate)
      ? `${Math.round(value * 100)}%`
      : `${Math.round(value)}`

export default function PostGateReason({
  findings,
  status,
  onRescore,
  rescoring,
  rescoreResult,
}: {
  findings: GateFinding[]
  status: string
  onRescore?: () => void
  rescoring?: boolean
  rescoreResult?: string | null
}) {
  // Always available on a pending post — that's where "edit, then re-score" lives, even for a
  // draft held before this feature recorded reasons.
  const pending = status === 'pending'
  if (!findings.length && !rescoreResult && !pending) return null
  const held = gateHold(findings)
  const isHeld = held.length > 0 && pending

  return (
    <div
      className={`rounded-lg border p-4 space-y-3 ${
        isHeld ? 'bg-amber-50 border-amber-200' : 'bg-gray-50 border-gray-200'
      }`}
    >
      <h4 className={`text-sm font-semibold ${isHeld ? 'text-amber-900' : 'text-gray-700'}`}>
        {isHeld ? '⏸ Why this is pending' : 'Quality checks'}
      </h4>

      {!findings.length && (
        <p className="text-sm text-gray-600">
          No quality gate is holding this post. Edit it and re-score to re-run the checks.
        </p>
      )}

      {findings.map((f) => {
        const score = fmtScore(f.gate, f.score)
        const threshold = fmtScore(f.gate, f.threshold)
        return (
          <div key={f.gate} className="space-y-1">
            <div className="flex items-center gap-2 flex-wrap">
              <span
                className={`px-2 py-0.5 rounded-full text-xs font-semibold ${
                  f.demoted ? 'bg-amber-200 text-amber-900' : 'bg-gray-200 text-gray-700'
                }`}
              >
                {f.label}
              </span>
              {score && (
                <span className="text-xs text-gray-500">
                  {f.gate === 'similarity' ? 'overlap' : 'score'} {score}
                  {threshold && ` · your limit ${threshold}`}
                </span>
              )}
              {!f.demoted && <span className="text-xs text-gray-400">advisory</span>}
            </div>
            <p className="text-sm text-gray-700">{f.explanation}</p>
            <p className="text-sm text-gray-600">
              <span className="font-medium">Fix: </span>
              {f.remediation}
            </p>
            {!!f.details?.length && (
              <ul className="text-xs text-gray-500 list-disc list-inside">
                {f.details.map((d, i) => (
                  <li key={i}>{d}</li>
                ))}
              </ul>
            )}
          </div>
        )
      })}

      <div className="flex flex-wrap items-center gap-2 pt-1">
        {onRescore && (
          <button
            type="button"
            onClick={onRescore}
            disabled={rescoring}
            className="bg-amber-600 text-white px-3 py-1.5 rounded-lg text-xs font-semibold hover:bg-amber-700 disabled:opacity-50 transition-colors"
          >
            {rescoring ? 'Re-scoring…' : 'Save & re-score'}
          </button>
        )}
        <Link
          to="/account#engagement"
          className="text-xs font-semibold text-blue-700 underline hover:text-blue-800"
        >
          Adjust thresholds
        </Link>
      </div>

      {rescoreResult && <p className="text-xs font-medium text-gray-700">{rescoreResult}</p>}
    </div>
  )
}
