import Toggle from '../../../components/Toggle'
import { maskProps } from '../../../utils/analytics'
import type { EngPrefs } from '../types'
import CsvInput from './CsvInput'
import { useEngagementPrefs } from './engagementPrefsCtx'
import { Advanced, Field, SectionCard, inputClass } from './Field'

type ListField = {
  field: keyof EngPrefs
  /** Sits behind the card's Advanced disclosure (the primary-control cap is already met). */
  advanced?: boolean
  /** Masked from session replay — free text the user cannot put a number to is content, not a filter. */
  masked?: boolean
}

const FILTERS: ListField[] = [
  { field: 'include_topics' },
  { field: 'exclude_topics' },
  { field: 'include_keywords' },
  { field: 'exclude_keywords' },
  { field: 'include_authors' },
  { field: 'exclude_authors' },
  // Issue #2047: the per-user forbidden-claim list — the same comma-separated control, but it
  // grades what LEM WRITES (posts and comments), not which posts it reads.
  { field: 'forbidden_claim_terms', advanced: true, masked: true },
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
          Saying so keeps a scan of LinkedIn's algorithmic feed from being read as a fresh one.
          The wording covers all three unsorted states without over-claiming: 'missing' means the
          control wasn't there, but 'top' means it was and wouldn't flip, and 'unknown' means the
          flip may well have worked and just couldn't be read back — asserting "the control wasn't
          available" there would be the same kind of untrue reading this line exists to prevent. */}
      {reach.feed_sort && reach.feed_sort !== 'recent' && reach.feed_sort !== 'n/a' && (
        <p className="mt-1" data-testid="feed-reach-unsorted">
          LEM couldn&rsquo;t confirm LinkedIn&rsquo;s &ldquo;Sort by → Recent&rdquo; order on this
          scan, so these posts may have come from your algorithmic feed rather than the newest ones.
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

  const listField = ({ field, masked }: ListField) => (
    <Field key={field} settingKey={field}>
      <CsvInput value={eng[field] as string[]}
        onChange={(values) => setEng({ [field]: values } as Partial<EngPrefs>)}
        placeholder="comma, separated, values"
        {...(masked ? maskProps(inputClass) : {})} />
    </Field>
  )

  return (
    <SectionCard
      title="Which posts LEM comments on"
      blurb="Comma-separated. Exclusions always win; if any include filter is set, a post must match one of them (keywords and authors literally, topics via AI relevance)."
    >
      <FeedReachFunnel />
      {FILTERS.filter((f) => !f.advanced).map(listField)}
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
      <Advanced>
        {FILTERS.filter((f) => f.advanced).map(listField)}
      </Advanced>
    </SectionCard>
  )
}
