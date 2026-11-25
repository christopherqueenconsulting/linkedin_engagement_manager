import Section, { SectionHeading } from './Section'

// The three-step section (issue #1300). Every strong page in this category has one and the old page
// did not, so a visitor had to infer the setup cost from a feature grid.
// Design pass r3: step 1 has no line under it (it only restated the title), and the section has
// no lede (it restated step 3).
const STEPS: { title: string; body?: string }[] = [
  {
    title: 'Sign in with LinkedIn',
  },
  {
    title: 'Set voice, topics and cadence',
    body: 'Tone, topics, the authors worth engaging with, and your posting days.',
  },
  {
    title: 'Approve posts, send the suggestions you like',
    body: 'A 30-day plan lands for review, and approved posts publish at their slots.',
  },
]

export default function HowItWorks() {
  return (
    <Section id="how-it-works" analyticsName="how_it_works" tone="tinted">
      <SectionHeading title="Three steps, then a short daily review" />
      {/* A true sequence, drawn as one: numbered stops on a single rule (horizontal from md,
          vertical on a phone), not three boxed cards (design pass 2026-11-24). */}
      <div className="relative">
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
              {step.body && <p className="mt-2 leading-relaxed text-fg-muted">{step.body}</p>}
            </li>
          ))}
        </ol>
      </div>
    </Section>
  )
}
