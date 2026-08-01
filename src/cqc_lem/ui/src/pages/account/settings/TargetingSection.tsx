import Toggle from '../../../components/Toggle'
import type { EngPrefs } from '../types'
import CsvInput from './CsvInput'
import { useEngagementPrefs } from './EngagementPrefsContext'
import { Field, SectionCard, inputClass } from './Field'

const FILTERS: [keyof EngPrefs, string][] = [
  ['include_topics', 'include_topics'],
  ['exclude_topics', 'exclude_topics'],
  ['include_keywords', 'include_keywords'],
  ['exclude_keywords', 'exclude_keywords'],
  ['include_authors', 'include_authors'],
  ['exclude_authors', 'exclude_authors'],
]

// The live funnel from the last real feed scan — so a targeting warning cites the user's own data
// instead of a hypothetical.
function FeedReachFunnel() {
  const { eng } = useEngagementPrefs()
  const reach = eng?.feed_reach
  if (!reach) return null
  return (
    <div className="rounded-lg bg-gray-50 border border-gray-100 p-3 text-xs text-gray-600" data-testid="feed-reach">
      <p className="font-medium text-gray-700 mb-1">Your last feed scan</p>
      <p>
        Examined <span className="font-semibold">{reach.examined}</span> posts →{' '}
        <span className="font-semibold">{reach.passed_filters}</span> passed recency / min-reactions →{' '}
        <span className="font-semibold">{reach.matched_topics}</span> matched your topics &amp; keywords →
        commented on <span className="font-semibold">{reach.commented}</span>
        {reach.fallback_used ? ' (used the fallback)' : ''}.
      </p>
      {(reach.roster_commented ?? 0) + (reach.feed_commented ?? 0) > 0 && (
        <p className="mt-1">
          <span className="font-semibold">{reach.roster_commented ?? 0}</span> from your roster
          {typeof reach.roster_targets_visited === 'number'
            ? ` (${reach.roster_targets_visited} accounts visited)`
            : ''}
          , <span className="font-semibold">{reach.feed_commented ?? 0}</span> from the feed.
        </p>
      )}
      {(reach.off_topic_skipped ?? 0) > 0 && (
        <p className="mt-1">
          Skipped <span className="font-semibold">{reach.off_topic_skipped}</span> off-topic post
          {reach.off_topic_skipped === 1 ? '' : 's'} — commenting off your focus topics hurts reach.
        </p>
      )}
      {/* LEM ranks candidates recency-first, which only holds if the feed was sorted by Recent.
          Saying so keeps a scan of LinkedIn's algorithmic feed from being read as a fresh one. */}
      {reach.feed_sort && reach.feed_sort !== 'recent' && reach.feed_sort !== 'n/a' && (
        <p className="mt-1" data-testid="feed-reach-unsorted">
          LinkedIn's &ldquo;Sort by → Recent&rdquo; control wasn&rsquo;t available on this scan, so
          these posts came from your algorithmic feed rather than the newest ones.
        </p>
      )}
    </div>
  )
}

// Targeting ONLY — catch-up, connections, video quality and link placement all moved to the
// sections that own them (F7).
export default function TargetingSection() {
  const { eng, setEng } = useEngagementPrefs()
  if (!eng) return null

  return (
    <SectionCard
      title="Which posts LEM comments on"
      blurb="Comma-separated. Exclusions always win; if any include filter is set, a post must match one of them (keywords and authors literally, topics via AI relevance)."
    >
      <FeedReachFunnel />
      {FILTERS.map(([field, key]) => (
        <Field key={key} settingKey={key}>
          <CsvInput value={eng[field] as string[]}
            onChange={(values) => setEng({ [field]: values } as Partial<EngPrefs>)}
            placeholder="comma, separated, values" />
        </Field>
      ))}
      <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
        <Field settingKey="min_reactions">
          <input type="number" min={0} value={eng.min_reactions ?? ''}
            onChange={(e) => setEng({ min_reactions: e.target.value === '' ? null : Number(e.target.value) })}
            placeholder="0" className={inputClass} />
        </Field>
        <Field settingKey="max_post_age_hours">
          <input type="number" min={1} value={eng.max_post_age_hours ?? ''}
            onChange={(e) => setEng({ max_post_age_hours: e.target.value === '' ? null : Number(e.target.value) })}
            placeholder="24" className={inputClass} />
        </Field>
      </div>
      <Field settingKey="feed_fallback_when_empty">
        <Toggle on={eng.feed_fallback_when_empty}
          onClick={() => setEng({ feed_fallback_when_empty: !eng.feed_fallback_when_empty })} />
      </Field>
    </SectionCard>
  )
}
