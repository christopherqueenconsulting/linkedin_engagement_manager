import { usePostHogStats, type PostHogStatsRow } from '../hooks/usePostHogStats'
import { formatRate } from './charts/palette'

function lastRow(rows: PostHogStatsRow[]): PostHogStatsRow | undefined {
  return rows.length ? rows[rows.length - 1] : undefined
}

function numeric(value: PostHogStatsRow[string] | undefined): number | null {
  return typeof value === 'number' ? value : null
}

// The in-SPA "your stats" panel (issue #654) — PostHog HogQL Endpoints instead of a bespoke MySQL
// reporting layer. Renders nothing while loading and nothing once loaded if every panel is
// unavailable (no key configured, or the endpoints haven't been provisioned yet), so an
// unconfigured deployment shows no half-empty card.
export default function PostHogStatsPanel() {
  const { data, isLoading } = usePostHogStats()
  if (isLoading || !data) return null

  const { posts_engagement: posts, comment_activity: comments, llm_cost_by_feature: llmCost } = data
  if (!posts.available && !comments.available && !llmCost.available) return null

  const latestPosts = lastRow(posts.rows)
  const latestComments = lastRow(comments.rows)
  const topFeatures = llmCost.rows.slice(0, 5)

  return (
    <div className="bg-white rounded-lg shadow-sm p-5">
      <h3 className="text-sm font-semibold text-gray-700 mb-3">Live stats</h3>
      <div className="grid grid-cols-1 sm:grid-cols-3 gap-4">
        <div>
          <p className="text-xs text-gray-500 mb-1">This week — posts</p>
          {posts.available && latestPosts ? (
            <>
              <p className="text-2xl font-bold text-gray-800">{numeric(latestPosts.posts_measured) ?? 0}</p>
              <p className="text-xs text-gray-500">
                {numeric(latestPosts.median_engagement_rate) != null
                  ? `${formatRate(numeric(latestPosts.median_engagement_rate)!)} median engagement`
                  : 'No engagement data yet'}
              </p>
            </>
          ) : (
            <p className="text-sm text-gray-400">Not available yet</p>
          )}
        </div>
        <div>
          <p className="text-xs text-gray-500 mb-1">This week — comments</p>
          {comments.available && latestComments ? (
            <>
              <p className="text-2xl font-bold text-gray-800">{numeric(latestComments.comments_measured) ?? 0}</p>
              <p className="text-xs text-gray-500">
                {numeric(latestComments.author_reply_pct) != null
                  ? `${latestComments.author_reply_pct}% author replies`
                  : 'No replies measured yet'}
              </p>
            </>
          ) : (
            <p className="text-sm text-gray-400">Not available yet</p>
          )}
        </div>
        <div>
          <p className="text-xs text-gray-500 mb-1">LLM cost by feature (30d)</p>
          {!llmCost.available ? (
            <p className="text-sm text-gray-400">Not available yet</p>
          ) : topFeatures.length > 0 ? (
            <ul className="text-xs text-gray-700 space-y-0.5">
              {topFeatures.map((row) => (
                <li key={String(row.feature)} className="flex justify-between gap-2">
                  <span className="truncate">{String(row.feature)}</span>
                  <span className="font-semibold flex-shrink-0">${(numeric(row.spend_usd) ?? 0).toFixed(2)}</span>
                </li>
              ))}
            </ul>
          ) : (
            <p className="text-sm text-gray-400">No LLM calls in the last 30 days</p>
          )}
        </div>
      </div>
    </div>
  )
}
