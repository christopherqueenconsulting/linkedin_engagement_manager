import { useState } from 'react'
import { Link, Navigate } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import api from '../api/client'
import { useAuth } from '../contexts/useAuth'
import { FLAGS, useFeatureFlags } from '../hooks/useFeatureFlags'

// The server never sends a zero for a number it could not read: it sends null and names the metric
// in `not_measured`. Rendering null as a dash or 0 would turn "unknown" into "nothing happened",
// so every value passes through `show`, which says so in words.
const UNMEASURED = 'Could not measure'

interface Action { key: string; text: string; link: string }
interface NotMeasured { key: string; label: string }
interface WeeklyReport {
  week_start: string
  week_end: string
  numbers: {
    posts_published: number | null
    median_impressions: number | null
    organic_engagement_rate: number | null
    follower_delta: number | null
  }
  deltas: Record<string, number | null>
  icp_share: { share: number; inside: number; n: number; engagers: number } | null
  comment_volume: { automated_total: number | null; assist_total: number | null } | null
  actions: Action[]
  not_measured: NotMeasured[]
}

interface IcpSegmentRow { segment_id: string; name: string; n: number; share_of_segmented: number | null; insufficient: boolean }
interface IcpMonthRow { month: string; n: number; share: number | null; gap: number | null; insufficient: boolean }
interface IcpReport {
  readable: boolean
  min_n: number
  overall: { engagers: number; n: number; share: number | null; insufficient: boolean } | null
  segments: IcpSegmentRow[]
  gap_by_month: IcpMonthRow[]
  not_measured: NotMeasured[]
}

function show(value: number | null | undefined, format: (v: number) => string = String): string {
  return value === null || value === undefined ? UNMEASURED : format(value)
}
const pct = (v: number) => `${Math.round(v * 100)}%`
const signed = (v: number) => (v > 0 ? `+${v}` : String(v))

function Metric({ label, value }: { label: string; value: string }) {
  const unknown = value === UNMEASURED
  return (
    <div className="rounded-panel border border-line bg-raised p-4">
      <dt className="text-sm text-fg-muted">{label}</dt>
      <dd className={`mt-1 ${unknown ? 'text-sm text-fg-muted' : 'text-2xl font-semibold text-fg'}`}>{value}</dd>
    </div>
  )
}

/**
 * The weekly 5-minute report, in-app (issue #2667), reading `GET /user/weekly-report`.
 * The flag only decides whether the page is offered; the server answers 404 behind it either way.
 */
export default function WeeklyReportPage() {
  const flags = useFeatureFlags()
  if (flags.isLoading) return <div className="h-40" aria-busy="true" />
  if (!flags.data?.flags[FLAGS.weeklyReport]) return <Navigate to="/" replace />
  return <ReportContent />
}

function ReportContent() {
  const { sessionToken } = useAuth()
  const report = useQuery({
    queryKey: ['weekly-report', sessionToken],
    queryFn: () =>
      api.get(`/user/weekly-report?session_token=${encodeURIComponent(sessionToken!)}`)
        .then((r) => r.data.detail as WeeklyReport),
    enabled: !!sessionToken,
    staleTime: 60 * 1000,
  })

  const header = (
    <div>
      <h1 className="text-section lg:text-section-lg font-semibold text-fg">Weekly report</h1>
      <p className="mt-2 text-sm text-fg-muted max-w-2xl">
        The last seven days at a glance. A number LEM could not read says so; it is never shown as zero.
      </p>
    </div>
  )

  if (report.isLoading) return <div className="space-y-6">{header}<div className="h-40" aria-busy="true" /></div>
  if (report.isError || !report.data) {
    return (
      <div className="space-y-6">
        {header}
        <p role="alert" className="text-sm text-danger">The report could not be loaded. Please try again later.</p>
      </div>
    )
  }

  const { numbers, icp_share: icp, comment_volume: volume } = report.data
  const allMissing = Object.values(numbers).every((v) => v === null)
  return (
    <div className="space-y-8">
      {header}
      {allMissing && (
        <p role="status" className="rounded-panel border border-warning/50 bg-warning/5 px-4 py-3 text-sm text-warning">
          {UNMEASURED} any of this week&apos;s numbers yet. The list below says which and why.
        </p>
      )}
      <dl className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <Metric label="Posts published" value={show(numbers.posts_published)} />
        <Metric label="Median impressions" value={show(numbers.median_impressions, (v) => Math.round(v).toLocaleString())} />
        <Metric label="Organic engagement rate" value={show(numbers.organic_engagement_rate, (v) => `${(v * 100).toFixed(1)}%`)} />
        <Metric label="Follower change" value={show(numbers.follower_delta, signed)} />
        <Metric
          label="Engagers who fit your ICP"
          value={icp ? `${pct(icp.share)} (n=${icp.n})` : UNMEASURED}
        />
        <Metric label="Automated comments" value={show(volume?.automated_total)} />
        <Metric label="Assist comments" value={show(volume?.assist_total)} />
      </dl>

      {report.data.actions.length > 0 && (
        <section aria-labelledby="wr-actions">
          <h2 id="wr-actions" className="text-lg font-semibold text-fg">Do next</h2>
          <ul className="mt-2 space-y-1 text-sm">
            {report.data.actions.map((a) => (
              <li key={a.key}><Link className="text-accent underline" to={a.link}>{a.text}</Link></li>
            ))}
          </ul>
        </section>
      )}

      {report.data.not_measured.length > 0 && (
        <section aria-labelledby="wr-unmeasured">
          <h2 id="wr-unmeasured" className="text-lg font-semibold text-fg">Not measured this week</h2>
          <ul className="mt-2 list-disc pl-5 text-sm text-fg-muted">
            {report.data.not_measured.map((m) => <li key={m.key}>{m.label}</li>)}
          </ul>
        </section>
      )}

      <IcpAudienceSection sessionToken={sessionToken} />
    </div>
  )
}

/** The ICP-audience report is on demand: nothing is read until the button is pressed. */
function IcpAudienceSection({ sessionToken }: { sessionToken: string | null | undefined }) {
  const [requested, setRequested] = useState(false)
  const icp = useQuery({
    queryKey: ['icp-audience-report', sessionToken],
    queryFn: () =>
      api.get(`/user/icp-audience-report?session_token=${encodeURIComponent(sessionToken!)}`)
        .then((r) => r.data.detail as IcpReport),
    enabled: requested && !!sessionToken,
    staleTime: 60 * 1000,
  })
  const data = icp.data

  return (
    <section aria-labelledby="icp-audience" className="space-y-3">
      <h2 id="icp-audience" className="text-lg font-semibold text-fg">Who engages versus who you want</h2>
      {!requested && (
        <button type="button" onClick={() => setRequested(true)}
          className="rounded-panel border border-line px-4 py-2 text-sm font-medium text-fg hover:bg-raised">
          Run the ICP audience report
        </button>
      )}
      {requested && icp.isLoading && <div className="h-24" aria-busy="true" />}
      {requested && (icp.isError || (data && !data.readable)) && (
        <p role="alert" className="text-sm text-danger">
          The audience report could not be read. This is not the same as no engagers.
        </p>
      )}
      {data?.readable && (
        <div className="space-y-4 text-sm">
          <p>
            {data.overall && !data.overall.insufficient
              ? `${pct(data.overall.share!)} of engagers with a fit reading fit your ICP (n=${data.overall.n} of ${data.overall.engagers}).`
              : `Insufficient data: n=${data.overall?.n ?? 0}, at least ${data.min_n} needed.`}
          </p>
          <div>
            <h3 className="font-medium text-fg">Top segments</h3>
            {data.segments.length === 0 ? (
              <p className="text-fg-muted">No engager is tied to a segment yet.</p>
            ) : (
              <ul className="mt-1 space-y-1">
                {data.segments.map((s) => (
                  <li key={s.segment_id}>
                    {s.name}: n={s.n}
                    {s.insufficient || s.share_of_segmented === null
                      ? ' (insufficient data)'
                      : ` (${pct(s.share_of_segmented)} of segmented engagers)`}
                  </li>
                ))}
              </ul>
            )}
          </div>
          <div>
            <h3 className="font-medium text-fg">Gap by month</h3>
            {data.gap_by_month.length === 0 ? (
              <p className="text-fg-muted">No month has a fit reading yet.</p>
            ) : (
              <ul className="mt-1 space-y-1">
                {data.gap_by_month.map((m) => (
                  <li key={m.month}>
                    {m.month}: n={m.n}
                    {m.insufficient || m.gap === null
                      ? ' (insufficient data)'
                      : `, ${pct(m.share!)} fit your ICP, ${pct(m.gap)} do not`}
                  </li>
                ))}
              </ul>
            )}
          </div>
          <ul className="list-disc pl-5 text-fg-muted">
            {data.not_measured.map((m) => <li key={m.key}>Not measured: {m.label}</li>)}
          </ul>
        </div>
      )}
    </section>
  )
}
