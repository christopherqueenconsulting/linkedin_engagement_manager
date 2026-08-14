import Icon, { type IconName } from './Icon'
import Section, { SectionHeading } from './Section'

// The named-problem section (issue #1300) — the most consistent section in this category and the
// one the old page had no version of. It goes between the proof strip and how-it-works: a visitor
// who does not recognise their own problem has no reason to read a feature list.
const PROBLEMS: { icon: IconName; title: string; body: string }[] = [
  {
    icon: 'slash',
    title: 'You post in bursts, then vanish',
    body: 'Three posts in a good week, nothing for a month. The feed rewards the people who never stop, and stopping costs more than starting did.',
  },
  {
    icon: 'clock',
    title: 'Publishing is the easy half',
    body: 'The reach comes from commenting, replying and following up — an hour a day of it, at times you are usually in a meeting.',
  },
  {
    icon: 'chat',
    title: 'Generic AI sounds like generic AI',
    body: 'A tool that writes "In today\'s fast-paced world" in your name costs you more credibility than silence would.',
  },
  {
    icon: 'shield',
    title: 'And automation makes you nervous',
    body: 'Reasonably. Rate limits, restrictions and blunt bots are real. Any tool worth using has to answer that before it asks for your session.',
  },
]

export default function ProblemSection() {
  return (
    <Section id="problem" analyticsName="problem" tone="white">
      <SectionHeading
        eyebrow="The problem"
        title="Showing up on LinkedIn is a daily job nobody has time for"
        lede="Not a content problem or an engagement problem — the same problem, twice a day, every weekday."
      />
      <div className="grid gap-6 md:grid-cols-2">
        {PROBLEMS.map(({ icon, title, body }) => (
          <div key={title} className="rounded-card border border-line-200 bg-surface-50 p-6">
            <span className="inline-flex h-10 w-10 items-center justify-center rounded-lg bg-white text-brand-600">
              <Icon name={icon} className="h-5 w-5" />
            </span>
            <h3 className="mt-4 text-title font-semibold text-ink-900">{title}</h3>
            <p className="mt-2 leading-relaxed text-ink-600">{body}</p>
          </div>
        ))}
      </div>
    </Section>
  )
}
