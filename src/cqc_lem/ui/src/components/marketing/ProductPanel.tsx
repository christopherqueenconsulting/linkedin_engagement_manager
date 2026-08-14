import type { ReactNode } from 'react'
import Icon from './Icon'

// The marketing page's product visuals (issue #1300).
//
// These are DOM-drawn diagrams of real LEM surfaces, not screenshots — no browser is reachable from
// the agent worktree that built this page, and inventing a screenshot is not an option. They carry
// the real vocabulary (post statuses, the approval step, the caps you set) and deliberately carry
// NO numbers: an engagement count in a mock is a fabricated number whether or not anyone reads it
// as one. The two figures that do appear are documented product defaults, labelled as defaults.
//
// Everything here is decorative to a screen reader — the surrounding copy already says what the
// panel shows — so the wrapper is labelled once and the innards are hidden.

const STATUS_TONE: Record<string, string> = {
  Approved: 'bg-success-700 text-white',
  'Pending review': 'bg-warning-700 text-white',
  Scheduled: 'bg-brand-600 text-white',
}

function Panel({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div
      role="img"
      aria-label={label}
      className="rounded-card border border-line-200 bg-white shadow-card overflow-hidden"
    >
      <div aria-hidden="true" className="flex items-center gap-2 border-b border-line-200 bg-surface-50 px-4 py-3">
        <span className="h-2.5 w-2.5 rounded-full bg-line-300" />
        <span className="h-2.5 w-2.5 rounded-full bg-line-300" />
        <span className="h-2.5 w-2.5 rounded-full bg-line-300" />
        <span className="ml-2 text-xs font-medium text-ink-500">Content Studio</span>
      </div>
      <div aria-hidden="true" className="p-4 space-y-3">
        {children}
      </div>
    </div>
  )
}

function Row({ title, status, meta }: { title: string; status: keyof typeof STATUS_TONE; meta: string }) {
  return (
    <div className="flex items-center gap-3 rounded-lg border border-line-200 px-3 py-3">
      <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg bg-brand-50 text-brand-600">
        <Icon name="pen" className="h-4 w-4" />
      </span>
      <span className="min-w-0 flex-1">
        <span className="block truncate text-sm font-medium text-ink-900">{title}</span>
        <span className="block text-xs text-ink-500">{meta}</span>
      </span>
      <span className={`shrink-0 rounded-full px-2.5 py-1 text-xs font-semibold ${STATUS_TONE[status]}`}>
        {status}
      </span>
    </div>
  )
}

export type PanelVariant = 'queue' | 'schedule' | 'engagement' | 'measure'

export default function ProductPanel({ variant }: { variant: PanelVariant }) {
  if (variant === 'schedule') {
    return (
      <Panel label="Diagram of the LEM scheduling calendar: a week of posting days with a draft placed in each golden-hour slot.">
        <div className="grid grid-cols-5 gap-2">
          {['Mon', 'Tue', 'Wed', 'Thu', 'Fri'].map((day, index) => (
            <div key={day} className="rounded-lg border border-line-200 p-2 text-center">
              <div className="text-xs font-semibold text-ink-500">{day}</div>
              <div
                className={`mt-2 h-10 rounded ${index % 2 === 0 ? 'bg-brand-100' : 'bg-surface-100'}`}
              />
            </div>
          ))}
        </div>
        <div className="flex items-center gap-2 rounded-lg bg-brand-50 px-3 py-2 text-xs text-ink-900">
          <Icon name="clock" className="h-4 w-4 text-brand-600" />
          Golden hour, in your timezone · 3 posts a week by default
        </div>
      </Panel>
    )
  }

  if (variant === 'engagement') {
    return (
      <Panel label="Diagram of a LEM feed comment: a draft reply waiting under a targeted post, with the day's comment cap shown beside it.">
        <div className="rounded-lg border border-line-200 p-3">
          <div className="h-2 w-1/3 rounded bg-surface-100" />
          <div className="mt-2 space-y-1.5">
            <div className="h-2 w-full rounded bg-surface-100" />
            <div className="h-2 w-4/5 rounded bg-surface-100" />
          </div>
          <div className="mt-3 rounded-lg border border-brand-100 bg-brand-50 p-3">
            <div className="flex items-center gap-2 text-xs font-semibold text-brand-600">
              <Icon name="chat" className="h-4 w-4" />
              Your comment, in your voice
            </div>
            <div className="mt-2 space-y-1.5">
              <div className="h-2 w-full rounded bg-brand-100" />
              <div className="h-2 w-2/3 rounded bg-brand-100" />
            </div>
          </div>
        </div>
        <div className="flex items-center gap-2 text-xs text-ink-500">
          <Icon name="gauge" className="h-4 w-4 text-brand-600" />
          Paced, and stopped by your own per-day cap
        </div>
      </Panel>
    )
  }

  if (variant === 'measure') {
    return (
      <Panel label="Diagram of the LEM measurement view: comment outcomes, post statistics and the suppression tripwire, each waiting on real data.">
        {['Comment outcomes at T+24h', 'Post and audience stats', 'Suppression tripwire'].map((row) => (
          <div
            key={row}
            className="flex items-center justify-between rounded-lg border border-line-200 px-3 py-3"
          >
            <span className="text-sm text-ink-700">{row}</span>
            <span className="text-sm font-semibold text-ink-500">from your account</span>
          </div>
        ))}
      </Panel>
    )
  }

  return (
    <Panel label="Diagram of the LEM approval queue: three drafts, one approved, one waiting for review and one scheduled.">
      <Row title="Why most LinkedIn advice fails consultants" status="Pending review" meta="Thought leadership · awareness" />
      <Row title="What we changed after last quarter" status="Approved" meta="Personal story · consideration" />
      <Row title="A 3-slide breakdown of the offer" status="Scheduled" meta="Carousel · decision" />
      <div className="flex items-center gap-2 rounded-lg bg-surface-50 px-3 py-2 text-xs text-ink-700">
        <Icon name="shield" className="h-4 w-4 text-brand-600" />
        Nothing publishes until you approve it
      </div>
    </Panel>
  )
}
