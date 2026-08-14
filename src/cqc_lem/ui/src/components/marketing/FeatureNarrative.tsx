import Icon, { IncludedMark } from './Icon'
import ProductPanel, { type PanelVariant } from './ProductPanel'
import Section, { SectionHeading } from './Section'

// The feature story, told one beat at a time with a product visual each (issue #1300), replacing a
// three-card emoji grid whose bullets described a product more general than the one that exists.
//
// Every line below names something shipped. The four beats are the actual order of the loop:
// write it, schedule it, engage with it, then measure what it earned.
const BEATS: {
  key: string
  eyebrow: string
  title: string
  body: string
  points: string[]
  panel: PanelVariant
}[] = [
  {
    key: 'content',
    eyebrow: 'Content',
    title: 'Content that sounds like you, not like a content tool',
    body: 'A voice profile distilled from your own posts and profile drives every draft, and the plan is built around a buyer journey rather than a topic list.',
    points: [
      'A 30-day plan across awareness, consideration and decision stages',
      'Thought leadership, industry commentary, personal story, carousels, native video and newsletters',
      'A 70/20/10 value / authority / promo mix, so the feed is not a pitch',
      'A quality contract and slop lint that block the tells before you ever see the draft',
    ],
    panel: 'queue',
  },
  {
    key: 'scheduling',
    eyebrow: 'Scheduling',
    title: 'Published when your audience is actually there',
    body: 'Slots are placed against your golden and peak hours in your own timezone, on the days you allow — not whenever the job happened to run.',
    points: [
      'Two to seven posts a week; three by default',
      'Posting days you choose — weekdays out of the box, weekends opt in',
      'Golden-hour placement measured from real publish time, never the time it was scheduled for',
      'Self-healing carousels and asset backfill, so a broken draft does not become a silent day',
    ],
    panel: 'schedule',
  },
  {
    key: 'engagement',
    eyebrow: 'Engagement',
    title: 'The daily hour, running without you',
    body: 'The half that actually earns reach: commenting on the right posts, answering the people who answer you, and following up.',
    points: [
      'Feed commenting against your topics, keywords, authors and minimum-reaction rules',
      'Replies on your own posts, plus a first comment under what you publish',
      'Appreciation and outreach DMs with multi-step follow-ups, each one approval-gated',
      'A weekly group post, roster targets and a paced company-page invite drip',
    ],
    panel: 'engagement',
  },
  {
    key: 'measurement',
    eyebrow: 'Measurement',
    title: 'You approve; then it measures what happened',
    body: 'Nothing here is a vanity chart. Each measure exists because something downstream reads it and changes behaviour.',
    points: [
      'Comment outcomes swept at T+24h, one row per comment',
      'Post and audience statistics pulled from your own account',
      'A suppression tripwire comparing you against your own trailing median, never a global average',
      'Reach demotion holds your feed commenting instead of quietly continuing',
    ],
    panel: 'measure',
  },
]

export default function FeatureNarrative() {
  return (
    <Section id="features" analyticsName="features" tone="white">
      <SectionHeading
        eyebrow="What it does"
        title="One loop: write, schedule, engage, measure"
        lede="Four beats, in the order they run. Everything named here exists today."
      />
      <div className="space-y-16 lg:space-y-24">
        {BEATS.map((beat, index) => (
          <article
            key={beat.key}
            data-testid={`feature-beat-${beat.key}`}
            className="grid items-center gap-8 lg:grid-cols-2 lg:gap-14"
          >
            <div className={index % 2 === 1 ? 'lg:order-2' : undefined}>
              <p className="text-sm font-semibold uppercase tracking-wide text-brand-600">
                {beat.eyebrow}
              </p>
              <h3 className="mt-3 text-headline font-bold text-ink-900">{beat.title}</h3>
              <p className="mt-4 leading-relaxed text-ink-600">{beat.body}</p>
              <ul className="mt-6 space-y-3">
                {beat.points.map((point) => (
                  <li key={point} className="flex items-start gap-3">
                    <IncludedMark included />
                    <span className="text-ink-700">{point}</span>
                  </li>
                ))}
              </ul>
            </div>
            <div className={index % 2 === 1 ? 'lg:order-1' : undefined}>
              <ProductPanel variant={beat.panel} />
            </div>
          </article>
        ))}
      </div>
      <p className="mt-12 flex items-start gap-3 rounded-card border border-line-200 bg-surface-50 p-5 text-sm text-ink-700">
        <span className="shrink-0 text-brand-600">
          <Icon name="users" className="h-5 w-5" />
        </span>
        Occasion and milestone posts are drafted for you to paste yourself — LinkedIn&apos;s
        celebrate composer has no API, and we would rather say so than pretend it is automated.
      </p>
    </Section>
  )
}
