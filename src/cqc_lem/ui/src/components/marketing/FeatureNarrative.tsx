import Icon, { IncludedMark } from './Icon'
import ProductPanel, { type PanelVariant } from './ProductPanel'
import Section, { SectionHeading } from './Section'

// The feature story, told one beat at a time with a product visual each (issue #1300), replacing a
// three-card emoji grid whose bullets described a product more general than the one that exists.
//
// Every line below names something shipped. The four beats are the actual order of the loop:
// write it, schedule it, engage with it, then measure what it earned. Since #2366 the engagement
// beat describes suggestions the user sends, never actions LEM takes on their behalf.
//
// Design pass r2: no small label above each beat (the section heading already names the four), and
// a point that the beat's own panel draws is not listed again beside it (the 30-day plan and the
// 70/20/10 mix, the two-to-seven weekly range, the reply suggestions).
const BEATS: {
  key: string
  title: string
  body: string
  points: string[]
  panel: PanelVariant
}[] = [
  {
    key: 'content',
    title: 'Content that sounds like you, not like a content tool',
    body: 'A voice profile distilled from your own posts and profile drives every draft, and the plan is built around a buyer journey rather than a topic list.',
    points: [
      'Thought leadership, industry commentary, personal story, carousels, native video and newsletters',
      'A quality contract and slop lint that block the tells before you ever see the draft',
    ],
    panel: 'plan',
  },
  {
    key: 'scheduling',
    title: 'Published when your audience is actually there',
    body: 'Slots are placed against your golden and peak hours in your own timezone, on the days you allow — not whenever the job happened to run.',
    points: [
      'Posting days you choose — weekdays out of the box, weekends opt in',
      'Golden-hour placement measured from real publish time, never the time it was scheduled for',
      'Self-healing carousels and asset backfill, so a broken draft does not become a silent day',
    ],
    panel: 'schedule',
  },
  {
    key: 'engagement',
    title: 'The daily hour, cut down to a review',
    body: 'The half that actually earns reach: commenting on the right posts, answering the people who answer you, and following up. LEM drafts these as suggestions; you review them and send the ones you want yourself.',
    points: [
      'Comment suggestions for posts that match your topics, keywords, authors and minimum-reaction rules',
      'Suggested appreciation and follow-up DMs',
    ],
    panel: 'engagement',
  },
  {
    key: 'measurement',
    title: 'You approve; then it shows what each post earned',
    body: 'Nothing here is a vanity chart: the numbers come from your own LinkedIn account.',
    points: [
      'Post and audience statistics pulled from your own account',
    ],
    panel: 'measure',
  },
]

export default function FeatureNarrative() {
  return (
    <Section id="features" analyticsName="features" tone="white">
      <SectionHeading
        title="One loop: write, schedule, engage, measure"
        lede="Four beats, in the order they run."
      />
      <div className="space-y-16 lg:space-y-24">
        {BEATS.map((beat, index) => (
          <article
            key={beat.key}
            data-testid={`feature-beat-${beat.key}`}
            className="grid items-center gap-8 lg:grid-cols-2 lg:gap-14"
          >
            {/* `min-w-0` on both columns for the same reason as the hero (issue #1556): without it
                the panel column's min-content width becomes the page's, and a phone lays the whole
                front page out wider than the screen and zooms out to fit. */}
            <div className={`min-w-0 ${index % 2 === 1 ? 'lg:order-2' : ''}`.trim()}>
              <h3 className="text-section font-semibold text-fg">{beat.title}</h3>
              <p className="mt-4 leading-relaxed text-fg-muted">{beat.body}</p>
              <ul className="mt-6 space-y-3">
                {beat.points.map((point) => (
                  <li key={point} className="flex items-start gap-3">
                    <IncludedMark included />
                    <span className="text-fg">{point}</span>
                  </li>
                ))}
              </ul>
            </div>
            <div className={`min-w-0 ${index % 2 === 1 ? 'lg:order-1' : ''}`.trim()}>
              <ProductPanel variant={beat.panel} />
            </div>
          </article>
        ))}
      </div>
      <p className="mt-12 flex max-w-measure items-start gap-3 rounded-panel border border-hairline bg-page p-5 text-sm text-fg">
        <span className="shrink-0 text-accent">
          <Icon name="users" className="h-5 w-5" />
        </span>
        Occasion and milestone posts are drafted for you to paste yourself — LinkedIn&apos;s
        celebrate composer has no API, and we would rather say so than pretend it is automated.
      </p>
    </Section>
  )
}
