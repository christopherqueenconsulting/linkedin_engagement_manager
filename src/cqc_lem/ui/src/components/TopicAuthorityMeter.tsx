// Topic Authority (Topic DNA) consistency meter — issue #384. 2026 LinkedIn ranking derives a
// 'Topic DNA' from the author's headline/about + declared focus topics and SUPPRESSES off-niche
// posts, so this surfaces — right in the post preview — how tightly a draft sits inside that niche.
// The score MIRRORS the backend deterministic heuristic in content_alignment.topic_authority_score
// (token-set overlap coefficient) so the meter roughly agrees with the server-side governor. It is
// advisory: nothing here blocks publishing.

import { OFF_NICHE_THRESHOLD, topicAuthorityScore } from '../utils/topicAuthority'

interface TopicAuthorityMeterProps {
  content: string
  focusTopics: string[]
  headline?: string
  about?: string
}

export default function TopicAuthorityMeter({
  content,
  focusTopics,
  headline,
  about,
}: TopicAuthorityMeterProps) {
  // No declared niche → nothing to score against; hide the meter rather than show a misleading 100%.
  if (!focusTopics || focusTopics.length === 0) return null
  if (!content.trim()) return null

  const score = topicAuthorityScore(content, focusTopics, headline, about)
  const pct = Math.round(score * 100)
  const offNiche = score < OFF_NICHE_THRESHOLD
  const strong = score >= 0.35

  const barColor = offNiche ? 'bg-red-500' : strong ? 'bg-green-500' : 'bg-amber-500'
  const label = offNiche ? 'Off-niche' : strong ? 'On-niche' : 'Loosely on-niche'
  const labelColor = offNiche ? 'text-red-600' : strong ? 'text-green-600' : 'text-amber-600'

  // Only claim the profile is part of the score when profile inputs are actually wired in.
  const scoredAgainst =
    headline?.trim() || about?.trim() ? 'focus topics and profile' : 'focus topics'

  return (
    <div className="mt-3 rounded-lg border border-gray-200 bg-gray-50 p-3">
      <div className="flex items-center justify-between mb-1">
        <p className="text-xs font-medium text-gray-700">Topic authority consistency</p>
        <span className={`text-xs font-semibold ${labelColor}`}>
          {label} · {pct}%
        </span>
      </div>
      <div className="h-2 w-full overflow-hidden rounded-full bg-gray-200" aria-hidden="true">
        <div
          className={`h-full rounded-full ${barColor} transition-all`}
          style={{ width: `${Math.max(pct, 3)}%` }}
        />
      </div>
      <p className="mt-1.5 text-[11px] leading-snug text-gray-500">
        {offNiche
          ? 'This draft drifts off your focus topics. 2026 LinkedIn suppresses off-niche posts — steer it back toward your Topic DNA.'
          : `How tightly this draft sits inside your ${scoredAgainst} — LinkedIn now rewards profile↔content consistency.`}
      </p>
    </div>
  )
}
