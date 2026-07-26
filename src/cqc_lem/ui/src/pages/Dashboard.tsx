import { useQuery } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import api from '../api/client'
import { useAuth } from '../contexts/AuthContext'
import { useUserTimezone } from '../hooks/useUserTimezone'
import { formatInTimezone } from '../utils/datetime'
import { isHttpUrl, commentsActivityUrl } from '../utils/links'
import OnboardingChecklist from '../components/OnboardingChecklist'
import LineChart, { type LinePoint } from '../components/charts/LineChart'
import Leaderboard, { type RankEntry } from '../components/charts/Leaderboard'
import { compactNumber, formatRate } from '../components/charts/palette'

interface PostStats {
  recommendations: { weekday: string; hour: number; avg_engagement: number; sample: number }[]
  rankings: Record<string, RankEntry[]>
  sample_size: number
}

interface PerPost {
  post_id: number
  scheduled_time: string | null
  format: string | null
  archetype: string | null
  hook_style: string | null
  topic: string | null
  buyer_stage: string | null
  reactions: number
  comments: number
  reposts: number
  saves: number
  impressions: number | null
  engagement: number
  engagement_rate: number | null
}

interface TrendPoint {
  date: string
  reactions: number
  comments: number
  reposts: number
  saves: number
  impressions: number | null
  engagement: number
  engagement_rate: number | null
  posts: number
}

// 70/20/10 content-mix compliance for the window (#618) — a property of the plan, not of stats.
interface ContentMix {
  counts: Record<string, number>
  total: number
  ratios: Record<string, number | null>
  target: Record<string, number>
  promo_max_ratio: number
  promo_every_n: number
  compliant: boolean
}

// Comment outcome tracking (#628). Every rate is nullable — an empty window has no reply rate, and
// rendering 0% would read as "nobody ever replies" rather than "nothing measured yet".
interface CommentQuality {
  days: number
  sample_size: number
  checked: number
  skipped: number
  author_reply_rate: number | null
  reply_rate: number | null
  like_rate: number | null
  demotion_rate: number | null
  visibility_sample: number
  verdict: { status: string; reason: string }
  hold: { active: boolean; reason: string | null; seconds_remaining: number }
}

// Content-quality telemetry (#630). One period of scored content plus the period before it — the
// comparison IS the signal, because a prompt/model swap regresses quality silently. Every metric is
// nullable and carries its own sample size: dimensions are measured on different subsets (engagement
// only where impressions were captured, similarity only where a history existed), and an unmeasured
// one must read as "—", never as zero.
interface QualityPeriod {
  items: number
  by_surface: Record<string, number>
  slop_sample: number
  slop_score_avg: number | null
  slop_hard_rate: number | null
  slop_hard_total: number
  similarity_sample: number
  similarity_avg: number | null
  similarity_max: number | null
  authenticity_sample: number
  authenticity_avg: number | null
  hook_sample: number
  hook_chars_avg: number | null
  hook_budget_rate: number | null
  hook_budget: number
  engagement_rate_sample: number
  engagement_rate: number | null
  impressions: number | null
  detector_sample: number
  detector_avg: number | null
}

interface ContentQualityAlert {
  name: string
  metric: string
  reason: string
  delta: number | null
  threshold: number
  sample: number
}

interface ContentQuality {
  days: number
  current: QualityPeriod
  prior: QualityPeriod
  deltas: Record<string, number | null>
  alerts: ContentQualityAlert[]
}

interface Analytics {
  per_post: PerPost[]
  trend: TrendPoint[]
  sample_size: number
  days: number
  content_mix?: ContentMix
  comment_quality?: CommentQuality
  content_quality?: ContentQuality
}

// Follower & audience telemetry (#627). Every count is nullable: a capture that could not read a
// number stores NULL ("not measured"), never 0.
interface AudiencePoint {
  date: string
  follower_count: number | null
  connection_count: number | null
  profile_views: number | null
  search_appearances: number | null
}

interface AudienceDelta {
  delta: number
  from: number
  to: number
  from_date: string
  pct: number | null
}

interface ActivityPoint {
  date: string
  posts: number
  comments: number
  replies: number
  dms: number
}

interface AudienceGrowth {
  series: AudiencePoint[]
  activity: ActivityPoint[]
  follower_count: number | null
  captured_at: string | null
  connection_count: number | null
  profile_views: number | null
  search_appearances: number | null
  deltas: Record<string, AudienceDelta | null>
  samples: number
  days: number
}

const MIX_LABELS: Record<string, string> = {
  value: 'Audience value',
  authority: 'Authority education',
  promo: 'Soft promo',
}

function formatPct(ratio: number | null | undefined): string {
  return ratio == null ? '—' : `${Math.round(ratio * 100)}%`
}

function formatScore(value: number | null | undefined, digits = 2): string {
  return value == null ? '—' : value.toFixed(digits)
}

// A quality delta is null until BOTH periods were measured. "no baseline" and "no change" are
// different facts, so the second period of a new account shows "vs — " rather than "vs 0.00".
function formatQualityDelta(value: number | null | undefined, digits = 2): string {
  if (value == null) return 'vs —'
  return `${value > 0 ? '+' : ''}${value.toFixed(digits)} vs last period`
}

// A follower delta is null until there is a baseline snapshot old enough to measure against —
// show "—" rather than inventing growth from a two-day history.
function formatDelta(d: AudienceDelta | null | undefined): string {
  if (!d) return '—'
  return `${d.delta > 0 ? '+' : ''}${d.delta.toLocaleString()}`
}

function deltaColor(d: AudienceDelta | null | undefined): string {
  if (!d || d.delta === 0) return 'text-gray-800'
  return d.delta > 0 ? 'text-green-600' : 'text-red-600'
}

// The baseline is the newest capture on or BEFORE the window edge, so after a run of failed
// captures a "7-day change" can really span longer. Name the date it was measured from rather than
// letting the tile imply a window it didn't cover.
function deltaSince(d: AudienceDelta | null | undefined): string {
  return d ? `since ${shortDate(d.from_date)}` : 'not enough history'
}

// "2026-07-20" → "Jul 20" for compact chart/table axes (dates are tz-agnostic calendar days).
const _MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']
function shortDate(iso: string): string {
  const [, m, d] = iso.split('-')
  const mi = Number(m) - 1
  return mi >= 0 && mi < 12 ? `${_MONTHS[mi]} ${Number(d)}` : iso
}

function titleCase(k: string | null): string {
  if (!k) return '—'
  const s = String(k).replace(/[_-]+/g, ' ').trim()
  return s.charAt(0).toUpperCase() + s.slice(1)
}

interface DashboardStats {
  scheduled_this_week: number
  pending_review: number
  posted_total: number
}

interface PlannedTask {
  kind: 'Post' | 'DM' | 'Newsletter'
  id: number
  title: string
  status: string
  scheduled_time: string
}

interface ActivityEntry {
  id: number
  action_type: string
  result: string
  post_id: number | null
  post_url: string | null
  message: string | null
  created_at: string
}

const ACTION_ICONS: Record<string, string> = {
  post: '📝',
  comment: '💬',
  reply: '↩️',
  dm: '✉️',
  engaged: '👍',
}

const KIND_ICONS: Record<string, string> = {
  Post: '📝',
  DM: '✉️',
  Newsletter: '📰',
}

const STATUS_COLORS: Record<string, string> = {
  APPROVED: 'bg-green-100 text-green-700',
  PENDING: 'bg-yellow-100 text-yellow-700',
  SCHEDULED: 'bg-blue-100 text-blue-700',
  POSTED: 'bg-purple-100 text-purple-700',
  PLANNING: 'bg-gray-100 text-gray-600',
}

function StatCard({ label, value, color }: { label: string; value: number; color: string }) {
  return (
    <div className={`bg-white rounded-lg p-5 border-l-4 ${color} shadow-sm`}>
      <p className="text-2xl font-bold text-gray-800">{value}</p>
      <p className="text-sm text-gray-500 mt-1">{label}</p>
    </div>
  )
}

export default function Dashboard() {
  const { user, sessionToken } = useAuth()
  const email = user?.email ?? ''
  const userTimezone = useUserTimezone()

  // Personalized best-times-to-post recommendations (read-only)
  const { data: postStats } = useQuery({
    queryKey: ['post-stats', sessionToken],
    queryFn: () =>
      api
        .get(`/user/post-stats?session_token=${encodeURIComponent(sessionToken!)}`)
        .then((r) => r.data.detail as PostStats),
    enabled: !!sessionToken,
    staleTime: 5 * 60 * 1000,
  })

  // Engagement-rate / impression trend + per-post performance, from captured post_stats (#395).
  const { data: analytics } = useQuery({
    queryKey: ['engagement-analytics', sessionToken],
    queryFn: () =>
      api
        .get(`/user/engagement-analytics?session_token=${encodeURIComponent(sessionToken!)}&days=90`)
        .then((r) => r.data.detail as Analytics),
    enabled: !!sessionToken,
    staleTime: 5 * 60 * 1000,
  })

  // Follower growth / profile views, overlaid with what we actually did each day (#627).
  const { data: audience } = useQuery({
    queryKey: ['audience-growth', sessionToken],
    queryFn: () =>
      api
        .get(`/user/audience-growth?session_token=${encodeURIComponent(sessionToken!)}&days=90`)
        .then((r) => r.data.detail as AudienceGrowth),
    enabled: !!sessionToken,
    staleTime: 5 * 60 * 1000,
  })

  const { data: statsData } = useQuery<{ detail: DashboardStats }>({
    queryKey: ['dashboard-stats', email],
    queryFn: () => api.get(`/dashboard/stats/?email=${encodeURIComponent(email)}`).then((r) => r.data),
    enabled: !!email,
    refetchInterval: 30_000,
  })

  // Upcoming (future-dated, non-terminal) work across posts, scheduled DMs, and newsletter
  // editions — the backend already filters terminal states, sorts soonest-first, and caps.
  const { data: plannedData } = useQuery<{ detail: { tasks: PlannedTask[] } }>({
    queryKey: ['planned-tasks', email],
    queryFn: () =>
      api.get(`/dashboard/planned-tasks/?email=${encodeURIComponent(email)}&limit=10`).then((r) => r.data),
    enabled: !!email,
    refetchInterval: 30_000,
  })

  const { data: activityData } = useQuery<{ detail: ActivityEntry[] }>({
    queryKey: ['activity', email],
    queryFn: () => api.get(`/activity/?email=${encodeURIComponent(email)}&limit=15`).then((r) => r.data),
    enabled: !!email,
    refetchInterval: 30_000,
  })

  // Home-feed comment rows carry a synthetic post_url (no real permalink); fall back to the
  // user's own "recent activity → comments" page derived from their stored LinkedIn profile URL.
  const { data: linkedinProfileUrl } = useQuery({
    queryKey: ['linkedin-profile', sessionToken],
    queryFn: () =>
      api
        .get(`/user/linkedin-profile?session_token=${encodeURIComponent(sessionToken!)}`)
        .then((r) => (r.data.detail?.linkedin_profile_url as string | null) ?? null),
    enabled: !!sessionToken,
    staleTime: 10 * 60 * 1000,
  })

  const commentsUrl = commentsActivityUrl(linkedinProfileUrl)

  const stats = statsData?.detail ?? { scheduled_this_week: 0, pending_review: 0, posted_total: 0 }

  const upcoming = plannedData?.detail?.tasks ?? []

  const activity = activityData?.detail ?? []

  const perPost = analytics?.per_post ?? []
  const trend = analytics?.trend ?? []
  const hasAnalytics = (analytics?.sample_size ?? 0) > 0
  const mix = analytics?.content_mix
  const commentQuality = analytics?.comment_quality
  const contentQuality = analytics?.content_quality

  const rateTrend: LinePoint[] = trend.map((t) => ({ x: shortDate(t.date), y: t.engagement_rate }))
  const impressionTrend: LinePoint[] = trend.map((t) => ({ x: shortDate(t.date), y: t.impressions }))

  // Audience growth panel. The follower line and the activity line share the same day window so
  // growth reads next to the work that drove it (two single-series charts, never a dual axis).
  const audienceSeries = audience?.series ?? []
  const activitySeries = audience?.activity ?? []
  const hasAudience = audienceSeries.length > 0
  // Both charts run over the UNION of days so they line up point-for-point; a day with no capture
  // is a gap in the follower line (null), not a zero.
  const audienceByDate = new Map(audienceSeries.map((p) => [p.date, p]))
  const activityByDate = new Map(activitySeries.map((a) => [a.date, a]))
  const audienceDays = [...new Set([...audienceByDate.keys(), ...activityByDate.keys()])].sort()
  const followerTrend: LinePoint[] = audienceDays.map((d) => ({
    x: shortDate(d),
    y: audienceByDate.get(d)?.follower_count ?? null,
  }))
  const activityTrend: LinePoint[] = audienceDays.map((d) => {
    const a = activityByDate.get(d)
    return { x: shortDate(d), y: a ? a.posts + a.comments + a.replies + a.dms : 0 }
  })

  const formatBoard = postStats?.rankings?.format ?? []
  const hookBoard = postStats?.rankings?.hook_style ?? []

  // Window totals for the KPI row.
  const totalImpressions = perPost.reduce((s, p) => s + (p.impressions ?? 0), 0)
  const totalEngagement = perPost.reduce((s, p) => s + p.engagement, 0)
  const rateComplete = perPost.length > 0 && perPost.every((p) => p.impressions != null && p.impressions > 0)
  const overallRate = rateComplete && totalImpressions > 0 ? totalEngagement / totalImpressions : null

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-bold text-gray-800">Dashboard</h1>
        <div className="flex gap-2">
          <Link
            to="/content"
            className="bg-blue-600 text-white px-4 py-2 rounded-lg text-sm font-semibold hover:bg-blue-700 transition-colors"
          >
            + Schedule Post
          </Link>
          <Link
            to="/content?tab=review"
            className="border border-gray-300 text-gray-600 px-4 py-2 rounded-lg text-sm font-semibold hover:bg-gray-50 transition-colors"
          >
            Review Posts
          </Link>
        </div>
      </div>

      {/* Activation checklist — hides itself once the user is activated */}
      <OnboardingChecklist />

      {/* Stats */}
      <div className="grid grid-cols-1 sm:grid-cols-3 gap-4">
        <StatCard label="Scheduled this week" value={stats.scheduled_this_week} color="border-blue-500" />
        <StatCard label="Pending review" value={stats.pending_review} color="border-yellow-500" />
        <StatCard label="Total posted" value={stats.posted_total} color="border-green-500" />
      </div>

      {/* Audience growth — follower/profile-view telemetry next to the activity that drove it (#627) */}
      {sessionToken && (
        <div className="bg-white rounded-lg shadow-sm border border-gray-200 p-6 space-y-6">
          <div className="flex items-baseline justify-between">
            <h2 className="text-lg font-semibold text-gray-700">Audience Growth</h2>
            <span className="text-xs text-gray-400">Last {audience?.days ?? 90} days</span>
          </div>

          {!hasAudience ? (
            <p className="text-sm text-gray-400 py-4 text-center">
              Gathering data — follower and profile-view tracking captures once a night from your
              LinkedIn session.
            </p>
          ) : (
            <>
              <div className="grid grid-cols-2 sm:grid-cols-5 gap-4">
                <div>
                  <p className="text-2xl font-bold text-gray-800">
                    {audience?.follower_count != null ? compactNumber(audience.follower_count) : '—'}
                  </p>
                  <p className="text-xs text-gray-500 mt-0.5">Followers</p>
                </div>
                {['7', '30'].map((w) => (
                  <div key={w}>
                    <p className={`text-2xl font-bold ${deltaColor(audience?.deltas?.[w])}`}>
                      {formatDelta(audience?.deltas?.[w])}
                    </p>
                    <p className="text-xs text-gray-500 mt-0.5">{w}-day change</p>
                    <p className="text-[10px] text-gray-400">{deltaSince(audience?.deltas?.[w])}</p>
                  </div>
                ))}
                <div>
                  <p className="text-2xl font-bold text-gray-800">
                    {audience?.profile_views != null ? compactNumber(audience.profile_views) : '—'}
                  </p>
                  <p className="text-xs text-gray-500 mt-0.5">Profile views</p>
                </div>
                <div>
                  <p className="text-2xl font-bold text-gray-800">
                    {audience?.connection_count != null ? compactNumber(audience.connection_count) : '—'}
                  </p>
                  <p className="text-xs text-gray-500 mt-0.5">Connections</p>
                </div>
              </div>

              <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
                <LineChart
                  title="Followers"
                  subtitle="Captured nightly from your own profile"
                  points={followerTrend}
                  format={compactNumber}
                  valueLabel="Followers"
                  emptyMessage="No follower count captured yet."
                />
                <LineChart
                  title="Engagement activity"
                  subtitle="Posts, comments, replies and DMs sent, by day"
                  points={activityTrend}
                  format={compactNumber}
                  valueLabel="Actions"
                  emptyMessage="No automation activity in this window."
                />
              </div>
              <p className="text-xs text-gray-400">
                Profile views and search appearances come from LinkedIn's own analytics surface — a
                blank means the capture couldn't read that number, not that it was zero.
              </p>
            </>
          )}
        </div>
      )}

      {/* Engagement analytics — trends, leaderboards, and per-post drill-down from post_stats (#395) */}
      {sessionToken && (
        <div className="bg-white rounded-lg shadow-sm border border-gray-200 p-6 space-y-6">
          <div className="flex items-baseline justify-between">
            <h2 className="text-lg font-semibold text-gray-700">Engagement Analytics</h2>
            <span className="text-xs text-gray-400">Last {analytics?.days ?? 90} days</span>
          </div>

          {/* Content mix governor (#618) — how much of the plan sells vs. gives value */}
          {mix && mix.total > 0 && (
            <div>
              <div className="flex items-baseline justify-between mb-2">
                <h3 className="text-sm font-semibold text-gray-700">
                  Content mix <span className="font-normal text-gray-400">(target 70/20/10)</span>
                </h3>
                <span
                  className={`text-xs px-2 py-0.5 rounded-full ${
                    mix.compliant ? 'bg-green-100 text-green-700' : 'bg-yellow-100 text-yellow-700'
                  }`}
                >
                  {mix.compliant
                    ? 'On policy'
                    : `Promo over ${formatPct(mix.promo_max_ratio)} ceiling`}
                </span>
              </div>
              <div className="grid grid-cols-3 gap-4">
                {['value', 'authority', 'promo'].map((k) => (
                  <div key={k}>
                    <p className="text-2xl font-bold text-gray-800">{formatPct(mix.ratios[k])}</p>
                    <p className="text-xs text-gray-500 mt-0.5">
                      {MIX_LABELS[k]} — {mix.counts[k] ?? 0} of {mix.total}
                    </p>
                    <p className="text-xs text-gray-400">target {formatPct(mix.target[k])}</p>
                  </div>
                ))}
              </div>
              <p className="text-xs text-gray-400 mt-2">
                One soft-promo post per {mix.promo_every_n} planned posts, case-study shaped with an
                artifact CTA — never a meeting ask.
                {(mix.counts.unclassified ?? 0) > 0 &&
                  ` ${mix.counts.unclassified} older post(s) predate the mix governor and are not counted.`}
              </p>
            </div>
          )}

          {/* Comment outcomes (#628) — did our comments earn replies, and are they still visible? */}
          {commentQuality && commentQuality.sample_size > 0 && (
            <div>
              <div className="flex items-baseline justify-between mb-2">
                <h3 className="text-sm font-semibold text-gray-700">
                  Comment outcomes{' '}
                  <span className="font-normal text-gray-400">
                    ({commentQuality.checked} checked, {commentQuality.skipped} not found)
                  </span>
                </h3>
                <span
                  className={`text-xs px-2 py-0.5 rounded-full ${
                    commentQuality.hold.active || commentQuality.verdict.status === 'hold'
                      ? 'bg-red-100 text-red-700'
                      : commentQuality.verdict.status === 'watch'
                        ? 'bg-yellow-100 text-yellow-700'
                        : 'bg-green-100 text-green-700'
                  }`}
                >
                  {commentQuality.hold.active ? 'Commenting held' : commentQuality.verdict.status}
                </span>
              </div>
              <div className="grid grid-cols-2 sm:grid-cols-4 gap-4">
                <div>
                  <p className="text-2xl font-bold text-gray-800">
                    {formatPct(commentQuality.author_reply_rate)}
                  </p>
                  <p className="text-xs text-gray-500 mt-0.5">Author replied</p>
                </div>
                <div>
                  <p className="text-2xl font-bold text-gray-800">{formatPct(commentQuality.reply_rate)}</p>
                  <p className="text-xs text-gray-500 mt-0.5">Earned any reply</p>
                </div>
                <div>
                  <p className="text-2xl font-bold text-gray-800">{formatPct(commentQuality.like_rate)}</p>
                  <p className="text-xs text-gray-500 mt-0.5">Earned a like</p>
                </div>
                <div>
                  <p className="text-2xl font-bold text-gray-800">
                    {formatPct(commentQuality.demotion_rate)}
                  </p>
                  <p className="text-xs text-gray-500 mt-0.5">
                    Demoted ({commentQuality.visibility_sample} readable)
                  </p>
                </div>
              </div>
              <p className="text-xs text-gray-400 mt-2">
                {commentQuality.hold.active
                  ? `Feed commenting is paused: ${commentQuality.hold.reason ?? 'comment quality'}.`
                  : commentQuality.verdict.reason}{' '}
                "Demoted" means the comment was absent from LinkedIn's default "Most relevant" view
                but still present under "Most recent"; comments we could not read either way are
                excluded rather than counted as healthy.
              </p>
            </div>
          )}

          {/* Content quality (#630) — is the WRITING drifting? A model or prompt swap regresses this
              silently, so every tile is shown against the previous period. */}
          {contentQuality && contentQuality.current.items > 0 && (
            <div>
              <div className="flex items-baseline justify-between mb-2">
                <h3 className="text-sm font-semibold text-gray-700">
                  Content quality{' '}
                  <span className="font-normal text-gray-400">
                    ({contentQuality.current.items} pieces scored, last {contentQuality.days} days)
                  </span>
                </h3>
                <span
                  className={`text-xs px-2 py-0.5 rounded-full ${
                    contentQuality.alerts.length > 0
                      ? 'bg-red-100 text-red-700'
                      : 'bg-green-100 text-green-700'
                  }`}
                >
                  {contentQuality.alerts.length > 0
                    ? `${contentQuality.alerts.length} regression${contentQuality.alerts.length > 1 ? 's' : ''}`
                    : 'steady'}
                </span>
              </div>
              <div className="grid grid-cols-2 sm:grid-cols-4 gap-4">
                <div>
                  <p className="text-2xl font-bold text-gray-800">
                    {formatScore(contentQuality.current.slop_score_avg)}
                  </p>
                  <p className="text-xs text-gray-500 mt-0.5">
                    AI-slop score ({contentQuality.current.slop_sample} linted)
                  </p>
                  <p className="text-[10px] text-gray-400">
                    {formatQualityDelta(contentQuality.deltas.slop_score_avg)}
                  </p>
                </div>
                <div>
                  <p className="text-2xl font-bold text-gray-800">
                    {formatScore(contentQuality.current.similarity_avg)}
                  </p>
                  <p className="text-xs text-gray-500 mt-0.5">
                    Self-similarity ({contentQuality.current.similarity_sample} compared)
                  </p>
                  <p className="text-[10px] text-gray-400">
                    {formatQualityDelta(contentQuality.deltas.similarity_avg)}
                  </p>
                </div>
                <div>
                  <p className="text-2xl font-bold text-gray-800">
                    {formatScore(contentQuality.current.authenticity_avg, 0)}
                  </p>
                  <p className="text-xs text-gray-500 mt-0.5">
                    Authenticity ({contentQuality.current.authenticity_sample} posts)
                  </p>
                  <p className="text-[10px] text-gray-400">
                    {formatQualityDelta(contentQuality.deltas.authenticity_avg, 1)}
                  </p>
                </div>
                <div>
                  <p className="text-2xl font-bold text-gray-800">
                    {contentQuality.current.engagement_rate == null
                      ? '—'
                      : `${(contentQuality.current.engagement_rate * 100).toFixed(2)}%`}
                  </p>
                  <p className="text-xs text-gray-500 mt-0.5">
                    Engagement / impression ({contentQuality.current.engagement_rate_sample} posts)
                  </p>
                  <p className="text-[10px] text-gray-400">
                    Hook within {contentQuality.current.hook_budget} chars:{' '}
                    {formatPct(contentQuality.current.hook_budget_rate)}
                  </p>
                </div>
              </div>
              {contentQuality.alerts.length > 0 ? (
                <ul className="text-xs text-red-600 mt-2 space-y-0.5">
                  {contentQuality.alerts.map((a) => (
                    <li key={a.name}>• {a.reason}</li>
                  ))}
                </ul>
              ) : (
                <p className="text-xs text-gray-400 mt-2">
                  Scored nightly across posts, comments and newsletter editions. A lower AI-slop score
                  and a lower self-similarity are better; both are compared against your own previous
                  period, so a prompt or model change that degrades the writing shows up here.
                </p>
              )}
            </div>
          )}

          {!hasAnalytics ? (
            <p className="text-sm text-gray-400 py-4 text-center">
              Gathering data — engagement analytics appear once your posted content has captured
              stats{analytics ? ` (currently ${analytics.sample_size})` : ''}.
            </p>
          ) : (
            <>
              {/* KPI row */}
              <div className="grid grid-cols-2 sm:grid-cols-4 gap-4">
                <div>
                  <p className="text-2xl font-bold text-gray-800">{perPost.length}</p>
                  <p className="text-xs text-gray-500 mt-0.5">Posts measured</p>
                </div>
                <div>
                  <p className="text-2xl font-bold text-gray-800">{compactNumber(totalImpressions)}</p>
                  <p className="text-xs text-gray-500 mt-0.5">Impressions</p>
                </div>
                <div>
                  <p className="text-2xl font-bold text-gray-800">{compactNumber(totalEngagement)}</p>
                  <p className="text-xs text-gray-500 mt-0.5">Engagement (weighted)</p>
                </div>
                <div>
                  <p className="text-2xl font-bold text-gray-800">
                    {overallRate != null ? formatRate(overallRate) : '—'}
                  </p>
                  <p className="text-xs text-gray-500 mt-0.5">Engagement rate</p>
                </div>
              </div>

              {/* Trends — two single-series charts (never a dual axis) */}
              <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
                <LineChart
                  title="Engagement rate"
                  subtitle="Weighted engagement per impression, by day posted"
                  points={rateTrend}
                  format={formatRate}
                  valueLabel="Engagement rate"
                  emptyMessage="No impression data yet — rate needs your own-view impressions."
                />
                <LineChart
                  title="Impressions"
                  subtitle="Total impressions on posts, by day posted"
                  points={impressionTrend}
                  format={compactNumber}
                  valueLabel="Impressions"
                  emptyMessage="No impression data captured yet."
                />
              </div>

              {/* Format & hook leaderboards (from /user/post-stats rankings) */}
              <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
                <Leaderboard title="Top formats" entries={formatBoard} humanizeKey={titleCase} />
                <Leaderboard title="Top hooks" entries={hookBoard} humanizeKey={titleCase} />
              </div>

              {/* Per-post performance drill-down */}
              <div>
                <h3 className="text-sm font-semibold text-gray-700 mb-2">Per-post performance</h3>
                <div className="overflow-x-auto">
                  <table className="w-full text-xs text-left tabular-nums">
                    <thead>
                      <tr className="text-gray-500 border-b border-gray-200">
                        <th className="py-1.5 pr-3 font-medium">Date</th>
                        <th className="py-1.5 pr-3 font-medium">Format</th>
                        <th className="py-1.5 pr-3 font-medium">Hook</th>
                        <th className="py-1.5 pr-3 font-medium text-right">Impr.</th>
                        <th className="py-1.5 pr-3 font-medium text-right">Reactions</th>
                        <th className="py-1.5 pr-3 font-medium text-right">Comments</th>
                        <th className="py-1.5 pr-3 font-medium text-right">Reposts</th>
                        <th className="py-1.5 pr-3 font-medium text-right">Saves</th>
                        <th className="py-1.5 font-medium text-right">Eng. rate</th>
                      </tr>
                    </thead>
                    <tbody>
                      {perPost.map((p) => (
                        <tr key={p.post_id} className="border-b border-gray-100 last:border-0 text-gray-700">
                          <td className="py-1.5 pr-3 whitespace-nowrap">
                            {p.scheduled_time ? shortDate(p.scheduled_time.slice(0, 10)) : '—'}
                          </td>
                          <td className="py-1.5 pr-3">{titleCase(p.format)}</td>
                          <td className="py-1.5 pr-3">{titleCase(p.hook_style)}</td>
                          <td className="py-1.5 pr-3 text-right">{p.impressions != null ? compactNumber(p.impressions) : '—'}</td>
                          <td className="py-1.5 pr-3 text-right">{p.reactions.toLocaleString()}</td>
                          <td className="py-1.5 pr-3 text-right">{p.comments.toLocaleString()}</td>
                          <td className="py-1.5 pr-3 text-right">{p.reposts.toLocaleString()}</td>
                          <td className="py-1.5 pr-3 text-right">{p.saves.toLocaleString()}</td>
                          <td className="py-1.5 text-right">{p.engagement_rate != null ? formatRate(p.engagement_rate) : '—'}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </div>
            </>
          )}
        </div>
      )}

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        {/* Planned Tasks — upcoming posts queue */}
        <div className="bg-white rounded-lg shadow-sm border border-gray-200 p-5">
          <div className="flex items-center justify-between mb-4">
            <h2 className="text-lg font-semibold text-gray-700">Planned Tasks</h2>
            <Link to="/content?tab=review" className="text-xs text-blue-600 hover:underline">
              Manage all
            </Link>
          </div>
          {upcoming.length === 0 ? (
            <div className="text-center py-6">
              <p className="text-sm text-gray-400">No upcoming tasks scheduled.</p>
              <Link
                to="/content?tab=review"
                className="text-xs text-blue-600 hover:underline mt-1 inline-block"
              >
                Generate weekly content →
              </Link>
            </div>
          ) : (
            <ul className="space-y-3">
              {upcoming.map((task) => (
                <li
                  key={`${task.kind}-${task.id}`}
                  className="flex items-start gap-3 p-3 rounded-lg bg-gray-50 hover:bg-gray-100 transition-colors"
                >
                  <div className="mt-0.5 text-lg flex-shrink-0">{KIND_ICONS[task.kind] ?? '🔔'}</div>
                  <div className="flex-1 min-w-0">
                    <div className="flex items-center gap-2 mb-0.5">
                      <span className="text-xs font-semibold uppercase tracking-wide text-gray-500">
                        {task.kind}
                      </span>
                      <span className="text-xs text-gray-400">
                        {formatInTimezone(task.scheduled_time, userTimezone)}
                      </span>
                    </div>
                    <p className="text-sm text-gray-700 line-clamp-2">{task.title}</p>
                  </div>
                  <span
                    className={`flex-shrink-0 px-2 py-0.5 rounded-full text-xs font-medium ${
                      STATUS_COLORS[task.status.toUpperCase()] ?? 'bg-gray-100 text-gray-600'
                    }`}
                  >
                    {task.status}
                  </span>
                </li>
              ))}
            </ul>
          )}
        </div>

        {/* Activity Feed — what has happened */}
        <div className="bg-white rounded-lg shadow-sm border border-gray-200 p-5">
          <h2 className="text-lg font-semibold text-gray-700 mb-4">Activity Feed</h2>
          {activity.length === 0 ? (
            <p className="text-sm text-gray-400 text-center py-6">
              No activity yet. Posts, comments, DMs, and replies will appear here.
            </p>
          ) : (
            <ul className="space-y-2 max-h-80 overflow-y-auto pr-1">
              {activity.map((entry) => (
                <li
                  key={entry.id}
                  className="flex items-start gap-3 py-2 border-b border-gray-100 last:border-0"
                >
                  <span className="text-base mt-0.5 flex-shrink-0">
                    {ACTION_ICONS[entry.action_type] ?? '🔔'}
                  </span>
                  <div className="flex-1 min-w-0">
                    <div className="flex items-center gap-2">
                      <span className="text-xs font-semibold text-gray-700 capitalize">
                        {entry.action_type}
                      </span>
                      <span
                        className={`text-xs px-1.5 py-0.5 rounded-full font-medium ${
                          entry.result === 'success'
                            ? 'bg-green-100 text-green-700'
                            : 'bg-red-100 text-red-600'
                        }`}
                      >
                        {entry.result}
                      </span>
                    </div>
                    {entry.message && (
                      <p className="text-xs text-gray-500 mt-0.5 line-clamp-1">{entry.message}</p>
                    )}
                    {isHttpUrl(entry.post_url) ? (
                      <a
                        href={entry.post_url}
                        target="_blank"
                        rel="noopener noreferrer"
                        className="text-xs text-blue-500 hover:underline truncate block"
                      >
                        {entry.post_url}
                      </a>
                    ) : commentsUrl && (entry.action_type === 'comment' || entry.action_type === 'reply') ? (
                      // Feed comments/replies have no permalink (post_url is blanked server-side) —
                      // link to the user's own LinkedIn "recent activity → comments" page instead.
                      <a
                        href={commentsUrl}
                        target="_blank"
                        rel="noopener noreferrer"
                        title="View your LinkedIn comments"
                        className="text-xs text-blue-500 hover:underline truncate block"
                      >
                        View your LinkedIn comments
                      </a>
                    ) : null}
                  </div>
                  <span className="text-xs text-gray-400 flex-shrink-0 whitespace-nowrap">
                    {formatInTimezone(entry.created_at, userTimezone)}
                  </span>
                </li>
              ))}
            </ul>
          )}
        </div>
      </div>

      {/* Best times to post (data-driven) */}
      {postStats && (
        <div className="bg-white rounded-lg shadow-sm border border-gray-200 p-6 space-y-3">
          <h2 className="text-base font-semibold text-gray-700">Your Best Times to Post</h2>
          {postStats.recommendations.length > 0 ? (
            <>
              <p className="text-xs text-gray-500">Learned from your own post engagement — scheduling leans toward these.</p>
              <ul className="text-sm text-gray-700 space-y-1">
                {postStats.recommendations.map((r, i) => (
                  <li key={i} className="flex justify-between">
                    <span>{r.weekday} @ {String(r.hour).padStart(2, '0')}:00</span>
                    <span className="text-gray-400">avg engagement {r.avg_engagement} · {r.sample} post(s)</span>
                  </li>
                ))}
              </ul>
            </>
          ) : (
            <p className="text-xs text-gray-500">Gathering data — recommendations appear after a few posts have engagement stats (currently {postStats.sample_size}).</p>
          )}
        </div>
      )}
    </div>
  )
}
