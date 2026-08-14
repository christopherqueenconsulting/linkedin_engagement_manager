import Section, { SectionHeading } from './Section'

// The three-step section (issue #1300). Every strong page in this category has one and the old page
// did not, so a visitor had to infer the setup cost from a feature grid.
const STEPS = [
  {
    title: 'Connect your LinkedIn session',
    body: 'Sign in once. LEM works from your own session behind your own static residential egress — not a shared bot farm, and not a scraper pointed at your network.',
  },
  {
    title: 'Set voice, targeting and caps',
    body: 'Tone and style, the topics and authors worth engaging, how many posts a week, which days, and the hard per-day ceilings for comments, DMs and invites.',
  },
  {
    title: 'Approve the first drafts — then it runs',
    body: 'A 30-day plan lands for review. Approve what you like, edit what you do not, and the daily loop picks up from there while you keep the veto.',
  },
]

export default function HowItWorks() {
  return (
    <Section id="how-it-works" analyticsName="how_it_works" tone="tinted">
      <SectionHeading
        eyebrow="How it works"
        title="Three steps, then it is a background job"
        lede="Setup is a sitting; after that the only recurring task is approving what you are shown."
      />
      <ol className="grid gap-6 md:grid-cols-3">
        {STEPS.map((step, index) => (
          <li key={step.title} className="rounded-card border border-line-200 bg-white p-6">
            <span
              aria-hidden="true"
              className="inline-flex h-10 w-10 items-center justify-center rounded-full bg-brand-50 text-lg font-bold text-brand-600"
            >
              {index + 1}
            </span>
            <h3 className="mt-4 text-title font-semibold text-ink-900">
              <span className="sr-only">{`Step ${index + 1}: `}</span>
              {step.title}
            </h3>
            <p className="mt-2 leading-relaxed text-ink-600">{step.body}</p>
          </li>
        ))}
      </ol>
    </Section>
  )
}
