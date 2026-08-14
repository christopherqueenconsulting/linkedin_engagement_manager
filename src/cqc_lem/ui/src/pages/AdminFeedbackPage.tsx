import { useState } from 'react'
import TableScroll from '../components/TableScroll'
import { useAuth } from '../contexts/useAuth'
import { useAdminFeedback } from '../hooks/useAdminFeedback'
import { useFeedbackReview } from '../hooks/useFeedbackReview'
import type { ReviewResult } from '../hooks/useFeedbackReview'

const PAGE_SIZE = 25

const STATUS_LABELS: Record<string, string> = {
  new: 'Pending review',
  triaged: 'Triaged',
  clustered: 'Clustered',
  issue_created: 'Issue created',
  resolved: 'Resolved',
  dismissed: 'Dismissed',
}

const SOURCE_LABELS: Record<string, string> = {
  widget: 'Widget',
  bug: 'Bug report',
  nps: 'NPS',
  review: 'Review',
  passive: 'Passive',
  csat: 'CSAT',
}

// What the filer did, in words an admin can act on (issue #1036). Approve used to report itself
// with the filer's raw verb in a grey line above the table — so an outcome that filed nothing and
// left the row exactly where it was read as "the button does nothing".
const FILING_OUTCOMES: Record<string, string> = {
  filed: 'Issue filed',
  deduped: 'Added as another report on the existing issue',
  dropped: 'Classified as noise — dismissed, no issue filed',
  faq: 'Routed to the FAQ queue — no issue filed',
  needs_human: 'Held for human triage — no issue filed',
  rate_limited: 'This reporter is over the daily issue cap — nothing filed',
  error: 'GitHub refused the filing — nothing changed. Try Approve again.',
}

// A `reason` that changes what the admin should DO next outranks the verb it arrived with: an
// `error` because the classifier never answered is not GitHub refusing, and a `dropped` because the
// report is empty is not a noise verdict. Both would otherwise be reported as something else.
const FILING_REASONS: Record<string, string> = {
  'classification unavailable':
    'The triage classifier could not be reached — nothing filed. Try Approve again.',
  'empty body': 'This report has no text — dismissed, no issue filed',
}

function outcomeText(result: ReviewResult | undefined): string | null {
  if (!result) return null
  if (result.action === 'dismissed') return 'Dismissed'
  const filing = result.filing_result?.action
  const reason = result.filing_result?.reason
  const label = (reason && FILING_REASONS[reason])
    || (filing && FILING_OUTCOMES[filing])
    || `Approved — filer: ${filing ?? 'done'}`
  const number = result.filing_result?.issue_number
  return number ? `${label} (#${number})` : label
}

function classNames(...classes: (string | false | null | undefined)[]) {
  return classes.filter(Boolean).join(' ')
}

export default function AdminFeedbackPage() {
  const { sessionToken } = useAuth()
  const [statusFilter, setStatusFilter] = useState<string>('')
  const [sourceFilter, setSourceFilter] = useState<string>('')
  const [page, setPage] = useState(0)

  const { data, isLoading, error, refetch } = useAdminFeedback({
    sessionToken: sessionToken || '',
    status: statusFilter || undefined,
    source: sourceFilter || undefined,
    limit: PAGE_SIZE,
    offset: page * PAGE_SIZE,
  })

  const review = useFeedbackReview()
  // Which row the last click belonged to, so the outcome can be shown AT the button (#1036). The
  // banner alone sits above the table — on a scrolled list the admin never sees it, and every
  // outcome that files nothing leaves the row untouched, so there is nothing else to notice.
  const [actedId, setActedId] = useState<number | null>(null)

  // `mutate`, not `mutateAsync`: an awaited rejection here had nobody to catch it, so a failed
  // approve (GitHub down, row already triaged by the beat) showed the admin nothing at all — the
  // row just stayed put. The hook invalidates the list on success, so no manual refetch either.
  function handleAction(id: number, action: 'approve' | 'dismiss') {
    setActedId(id)
    review.mutate({ feedbackId: id, action, sessionToken: sessionToken || '' })
  }

  const items = data?.items ?? []
  const reviewError = review.error
    ? ((review.error as { response?: { data?: { detail?: string } } })?.response?.data?.detail
       || (review.error instanceof Error ? review.error.message : 'unknown error'))
    : null
  const outcome = reviewError ? null : outcomeText(review.data)
  // An approve that filed nothing left the row in `new`, so the table is about to re-render
  // identically — the message IS the whole feedback and it must not read as a success.
  const outcomeFailed = review.isSuccess && review.data?.action === 'approved'
    && review.data?.filed === false

  return (
    <div className="max-w-5xl space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold text-gray-800">Feedback triage</h1>
          <p className="text-sm text-gray-500">
            Approve feedback for the auto-work pipeline or dismiss it. Only feedback from admin users is auto-triaged without review.
          </p>
        </div>
      </div>

      <div className="flex flex-wrap gap-3">
        <div className="flex items-center gap-2">
          <label htmlFor="status-filter" className="text-sm text-gray-600">Status</label>
          <select
            id="status-filter"
            value={statusFilter}
            onChange={(e) => { setStatusFilter(e.target.value); setPage(0) }}
            className="text-sm border border-gray-300 rounded-lg px-2 py-1"
          >
            <option value="">All</option>
            {Object.entries(STATUS_LABELS).map(([value, label]) => (
              <option key={value} value={value}>{label}</option>
            ))}
          </select>
        </div>
        <div className="flex items-center gap-2">
          <label htmlFor="source-filter" className="text-sm text-gray-600">Source</label>
          <select
            id="source-filter"
            value={sourceFilter}
            onChange={(e) => { setSourceFilter(e.target.value); setPage(0) }}
            className="text-sm border border-gray-300 rounded-lg px-2 py-1"
          >
            <option value="">All</option>
            {Object.entries(SOURCE_LABELS).map(([value, label]) => (
              <option key={value} value={value}>{label}</option>
            ))}
          </select>
        </div>
        <button
          onClick={() => void refetch()}
          disabled={isLoading}
          className="text-sm text-blue-600 hover:text-blue-800 font-medium disabled:opacity-50"
        >
          Refresh
        </button>
      </div>

      {error && (
        <p className="text-sm text-red-600">
          Could not load feedback: {error instanceof Error ? error.message : 'unknown error'}
        </p>
      )}

      {reviewError && (
        <p className="text-sm text-red-600">Could not record the review: {reviewError}</p>
      )}

      {/* Approving does not guarantee an issue: the filer can drop, FAQ, or fail on GitHub and
          leave the row where it was. Show what actually happened, in those words. */}
      {outcome && (
        <p className={classNames('text-sm', outcomeFailed ? 'text-amber-700' : 'text-gray-600')}>
          Last review: {outcome}
        </p>
      )}

      <div className="bg-white border border-gray-200 rounded-xl shadow-sm overflow-hidden">
        <TableScroll label="Feedback submissions" minWidth={900} testId="feedback-table">
          <table className="w-full text-sm text-left">
            <thead className="bg-gray-50 text-gray-600 font-medium">
              <tr>
                <th className="px-4 py-3">ID</th>
                <th className="px-4 py-3">Source</th>
                <th className="px-4 py-3">Reporter</th>
                <th className="px-4 py-3">Body</th>
                <th className="px-4 py-3">Status</th>
                <th className="px-4 py-3">Issue</th>
                <th className="px-4 py-3">Submitted</th>
                <th className="px-4 py-3 text-right">Actions</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-100">
              {items.map((item) => (
                <tr key={item.id} className="hover:bg-gray-50">
                  <td className="px-4 py-3 text-gray-500">{item.id}</td>
                  <td className="px-4 py-3">{SOURCE_LABELS[item.source] ?? item.source}</td>
                  <td className="px-4 py-3">
                    {item.email ? (
                      <span className={classNames('text-gray-800', item.is_admin_reporter && 'font-semibold')}>
                        {item.email}
                        {item.is_admin_reporter && <span className="ml-1 text-xs text-blue-600">(admin)</span>}
                      </span>
                    ) : (
                      <span className="text-gray-400 italic">Anonymous</span>
                    )}
                  </td>
                  <td className="px-4 py-3 max-w-xs truncate" title={item.body}>{item.body}</td>
                  <td className="px-4 py-3">
                    <span className={classNames(
                      'inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium',
                      item.status === 'new' && 'bg-amber-100 text-amber-800',
                      item.status === 'dismissed' && 'bg-gray-100 text-gray-600',
                      item.status === 'issue_created' && 'bg-green-100 text-green-800',
                      !['new', 'dismissed', 'issue_created'].includes(item.status) && 'bg-blue-100 text-blue-800'
                    )}>
                      {STATUS_LABELS[item.status] ?? item.status}
                    </span>
                  </td>
                  <td className="px-4 py-3">
                    {item.github_issue_number ? (
                      <a
                        href={`https://github.com/christopherqueenconsulting/linkedin_engagement_manager/issues/${item.github_issue_number}`}
                        target="_blank"
                        rel="noreferrer"
                        className="text-blue-600 hover:underline"
                      >
                        #{item.github_issue_number}
                      </a>
                    ) : (
                      <span className="text-gray-400">—</span>
                    )}
                  </td>
                  <td className="px-4 py-3 text-gray-500">
                    {item.created_at ? new Date(item.created_at).toLocaleString() : '—'}
                  </td>
                  <td className="px-4 py-3 text-right">
                    {item.status === 'new' && (
                      <div className="flex flex-col items-end gap-1">
                        <div className="flex items-center justify-end gap-2">
                          <button
                            onClick={() => handleAction(item.id, 'approve')}
                            disabled={review.isPending}
                            className="text-xs bg-blue-600 text-white px-2.5 py-1 rounded hover:bg-blue-700 disabled:opacity-50"
                          >
                            {review.isPending && actedId === item.id ? 'Working…' : 'Approve'}
                          </button>
                          <button
                            onClick={() => handleAction(item.id, 'dismiss')}
                            disabled={review.isPending}
                            className="text-xs bg-gray-100 text-gray-700 px-2.5 py-1 rounded hover:bg-gray-200 disabled:opacity-50"
                          >
                            Dismiss
                          </button>
                        </div>
                        {actedId === item.id && !review.isPending && (reviewError || outcome) && (
                          <p className={classNames(
                            'text-xs max-w-xs text-right',
                            reviewError || outcomeFailed ? 'text-amber-700' : 'text-gray-500'
                          )}>
                            {reviewError || outcome}
                          </p>
                        )}
                      </div>
                    )}
                  </td>
                </tr>
              ))}
              {items.length === 0 && !isLoading && (
                <tr>
                  <td colSpan={8} className="px-4 py-8 text-center text-gray-500">
                    No feedback submissions match the current filters.
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </TableScroll>
      </div>

      <div className="flex items-center justify-between text-sm">
        <button
          onClick={() => setPage((p) => Math.max(0, p - 1))}
          disabled={page === 0 || isLoading}
          className="text-blue-600 hover:text-blue-800 disabled:opacity-50"
        >
          Previous
        </button>
        <span className="text-gray-600">Page {page + 1}</span>
        <button
          onClick={() => setPage((p) => p + 1)}
          disabled={items.length < PAGE_SIZE || isLoading}
          className="text-blue-600 hover:text-blue-800 disabled:opacity-50"
        >
          Next
        </button>
      </div>
    </div>
  )
}
