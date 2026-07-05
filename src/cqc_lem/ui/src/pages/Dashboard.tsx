import { useQuery } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import api from '../api/client'
import { useAuth } from '../contexts/AuthContext'
import { useUserTimezone } from '../hooks/useUserTimezone'
import { formatInTimezone } from '../utils/datetime'
import { isHttpUrl } from '../utils/links'

interface PostStats {
  recommendations: { weekday: string; hour: number; avg_engagement: number; sample: number }[]
  sample_size: number
}

interface DashboardStats {
  scheduled_this_week: number
  pending_review: number
  posted_total: number
}

interface Post {
  post_id: number
  content: string
  scheduled_time: string
  post_type: string
  status: string
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

  const { data: statsData } = useQuery<{ detail: DashboardStats }>({
    queryKey: ['dashboard-stats', email],
    queryFn: () => api.get(`/dashboard/stats/?email=${encodeURIComponent(email)}`).then((r) => r.data),
    enabled: !!email,
    refetchInterval: 30_000,
  })

  const { data: postsData } = useQuery<{ detail: { posts: Post[]; total: number } }>({
    queryKey: ['dashboard-posts', email],
    queryFn: () => api.get(`/posts/?email=${encodeURIComponent(email)}&page_size=50`).then((r) => r.data),
    enabled: !!email,
    refetchInterval: 30_000,
  })

  const { data: activityData } = useQuery<{ detail: ActivityEntry[] }>({
    queryKey: ['activity', email],
    queryFn: () => api.get(`/activity/?email=${encodeURIComponent(email)}&limit=15`).then((r) => r.data),
    enabled: !!email,
    refetchInterval: 30_000,
  })

  const stats = statsData?.detail ?? { scheduled_this_week: 0, pending_review: 0, posted_total: 0 }

  const allPosts = postsData?.detail?.posts ?? []
  const upcoming = allPosts
    .filter((p) => p.status !== 'POSTED')
    .sort((a, b) => new Date(a.scheduled_time).getTime() - new Date(b.scheduled_time).getTime())
    .slice(0, 5)

  const activity = activityData?.detail ?? []

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-bold text-gray-800">Dashboard</h1>
        <div className="flex gap-2">
          <Link
            to="/schedule"
            className="bg-blue-600 text-white px-4 py-2 rounded-lg text-sm font-semibold hover:bg-blue-700 transition-colors"
          >
            + Schedule Post
          </Link>
          <Link
            to="/review"
            className="border border-gray-300 text-gray-600 px-4 py-2 rounded-lg text-sm font-semibold hover:bg-gray-50 transition-colors"
          >
            Review Posts
          </Link>
        </div>
      </div>

      {/* Stats */}
      <div className="grid grid-cols-1 sm:grid-cols-3 gap-4">
        <StatCard label="Scheduled this week" value={stats.scheduled_this_week} color="border-blue-500" />
        <StatCard label="Pending review" value={stats.pending_review} color="border-yellow-500" />
        <StatCard label="Total posted" value={stats.posted_total} color="border-green-500" />
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        {/* Planned Tasks — upcoming posts queue */}
        <div className="bg-white rounded-lg shadow-sm border border-gray-200 p-5">
          <div className="flex items-center justify-between mb-4">
            <h2 className="text-lg font-semibold text-gray-700">Planned Tasks</h2>
            <Link to="/review" className="text-xs text-blue-600 hover:underline">
              Manage all
            </Link>
          </div>
          {upcoming.length === 0 ? (
            <div className="text-center py-6">
              <p className="text-sm text-gray-400">No upcoming tasks scheduled.</p>
              <Link
                to="/review"
                className="text-xs text-blue-600 hover:underline mt-1 inline-block"
              >
                Generate weekly content →
              </Link>
            </div>
          ) : (
            <ul className="space-y-3">
              {upcoming.map((post) => (
                <li
                  key={post.post_id}
                  className="flex items-start gap-3 p-3 rounded-lg bg-gray-50 hover:bg-gray-100 transition-colors"
                >
                  <div className="mt-0.5 text-lg flex-shrink-0">
                    {post.post_type === 'video'
                      ? '🎬'
                      : post.post_type === 'carousel'
                      ? '🎠'
                      : '📝'}
                  </div>
                  <div className="flex-1 min-w-0">
                    <p className="text-xs text-gray-500 mb-0.5">
                      {formatInTimezone(post.scheduled_time, userTimezone)}
                    </p>
                    <p className="text-sm text-gray-700 line-clamp-2">{post.content}</p>
                  </div>
                  <span
                    className={`flex-shrink-0 px-2 py-0.5 rounded-full text-xs font-medium ${
                      STATUS_COLORS[post.status] ?? 'bg-gray-100 text-gray-600'
                    }`}
                  >
                    {post.status}
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
                    {entry.post_url &&
                      (isHttpUrl(entry.post_url) ? (
                        <a
                          href={entry.post_url}
                          target="_blank"
                          rel="noopener noreferrer"
                          className="text-xs text-blue-500 hover:underline truncate block"
                        >
                          {entry.post_url}
                        </a>
                      ) : (
                        <span className="text-xs text-gray-400 truncate block">{entry.post_url}</span>
                      ))}
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
