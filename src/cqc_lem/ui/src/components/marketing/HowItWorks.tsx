import Section, { SectionHeading } from './Section'

// The three-step section (issue #1300). Every strong page in this category has one and the old page
// did not, so a visitor had to infer the setup cost from a feature grid.
const STEPS = [
  {
    title: 'Sign in with LinkedIn',
    body: 'Connect with LinkedIn’s own sign-in.',
  },
  {
    title: 'Set voice, topics and cadence',
    body: 'Tone and style, the topics and authors worth engaging with, how many posts a week, and which days.',
  },
  {
    title: 'Approve posts, send the suggestions you like',
    body: 'A 30-day plan lands for review. Approve what you like and edit what you do not; approved posts publish at their slots. Comment and reply suggestions wait for you to send them.',
  },
]

export default function HowItWorks() {
  return (
    <Section id="how-it-works" analyticsName="how_it_works" tone="tinted">
      <SectionHeading
        title="Three steps, then a short daily review"
        lede="Setup is a sitting; after that the recurring task is approving posts and choosing which suggestions to send."
      />
      {/* A true sequence, drawn as one: numbered stops on a single rule (horizontal from md,
          vertical on a phone), not three boxed cards (design pass 2026-11-24). */}
      <div className="relative">
        {/* Step connector rule. From md up the three steps sit in a 3-column grid with two 2rem
            gaps (4rem in all). The rule starts at the first marker's centre (left-5) and stops at
            the last marker's centre: one column width ((100% - 4rem) / 3) in from the right edge,
            less the marker's 1.25rem half-width. */}
        <span
          aria-hidden="true"
          className="absolute left-5 top-5 hidden h-px bg-hairline md:block md:right-[calc((100%_-_4rem)/3_-_1.25rem)]"
        />
        <ol className="relative grid gap-10 md:grid-cols-3 md:gap-8">
          {STEPS.map((step, index) => (
            <li key={step.title} className="relative pl-16 md:pl-0">
              {/* Phone: a vertical connector from this stop to the next, so the rule ends on step 3. */}
              {index < STEPS.length - 1 && (
                <span aria-hidden="true" className="absolute left-5 top-10 h-full w-px bg-hairline md:hidden" />
              )}
              <span
                aria-hidden="true"
                className="absolute left-0 top-0 inline-flex h-10 w-10 items-center justify-center rounded-full bg-accent text-base font-semibold text-on-accent md:static"
              >
                {index + 1}
              </span>
              <h3 className="text-title font-semibold text-fg md:mt-6">
                <span className="sr-only">{`Step ${index + 1}: `}</span>
                {step.title}
              </h3>
              <p className="mt-2 leading-relaxed text-fg-muted">{step.body}</p>
            </li>
          ))}
        </ol>
      </div>
    </Section>
  )
}
