import Icon from './Icon'
import ProductPanel, { type PanelVariant } from './ProductPanel'
import Section, { SectionHeading } from './Section'

// The feature story, told one beat at a time with a product visual each (issue #1300), replacing a
// three-card emoji grid whose bullets described a product more general than the one that exists.
//
// Every line below names something shipped. The four beats are the actual order of the loop:
// write it, schedule it, engage with it, then measure what it earned. Since #2366 the engagement
// beat describes suggestions the user sends, never actions LEM takes on their behalf.
//
// Design pass r2: no small label above each beat (the section heading already names the four).
// Design pass r3: one sentence per beat and no bullet list, so the section holds about 60 body words
// (density budget). The beat's own panel carries the detail; the safety ledger carries the quality
// gate. The cut bullets are listed in the design plan for the spec owner's sign-off.
const BEATS: {
  key: string
  title: string
  body: string
  panel: PanelVariant
}[] = [
  {
    key: 'content',
    title: 'Content that sounds like you, not like a content tool',
    body: 'A voice profile distilled from your own posts and profile drives every draft.',
    panel: 'plan',
  },
  {
    key: 'scheduling',
    title: 'Published when your audience is actually there',
    body: 'Slots land in your golden and peak hours, in your own timezone, on the days you allow.',
    panel: 'schedule',
  },
  {
    key: 'engagement',
    title: 'The daily hour, cut down to a review',
    body: 'LEM drafts comments, replies and follow-up DMs as suggestions; you send the ones you want yourself.',
    panel: 'engagement',
  },
  {
    key: 'measurement',
    title: 'You approve; then it shows what each post earned',
    body: 'The numbers come from your own LinkedIn account, not a vanity chart.',
    panel: 'measure',
  },
]

export default function FeatureNarrative() {
  return (
    <Section id="features" analyticsName="features" tone="white">
      <SectionHeading
        title="One loop: write, schedule, engage, measure"
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
              <p className="mt-4 max-w-measure leading-relaxed text-fg-muted">{beat.body}</p>
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
        Occasion and milestone posts are drafted for you to paste yourself, because LinkedIn&apos;s
        celebrate composer has no API.
      </p>
    </Section>
  )
}
