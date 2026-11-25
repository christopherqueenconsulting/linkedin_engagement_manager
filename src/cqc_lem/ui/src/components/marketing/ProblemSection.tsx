import Section, { SectionHeading } from './Section'

// The named-problem section (issue #1300) — the most consistent section in this category and the
// one the old page had no version of. It goes between the proof strip and how-it-works: a visitor
// who does not recognise their own problem has no reason to read a feature list.
// One short line under each statement (design pass r2): the cut sentences restated the heading's
// "daily job" and the comparison's "an hour a day", or the safety section's answer.
const PROBLEMS: { title: string; body: string }[] = [
  {
    title: 'You post in bursts, then vanish',
    body: 'Three posts in a good week, nothing for a month.',
  },
  {
    title: 'Publishing is the easy half',
    body: 'The reach comes from commenting, replying and following up.',
  },
  {
    title: 'Generic AI sounds like generic AI',
    body: 'A tool that writes "In today\'s fast-paced world" in your name costs you more credibility than silence would.',
  },
  {
    title: 'And automation makes you nervous',
    body: 'Rate limits, restrictions and blunt bots are real.',
  },
]

export default function ProblemSection() {
  return (
    <Section id="problem" analyticsName="problem" tone="white">
      <SectionHeading
        title="Showing up on LinkedIn is a daily job nobody has time for"
        lede="Not a content problem or an engagement problem — the same problem, twice a day, every weekday."
      />
      {/* Plain statements on hairline rules, matching the proof strip, not a grid of icon cards. */}
      <div className="grid gap-x-12 md:grid-cols-2">
        {PROBLEMS.map(({ title, body }) => (
          <div key={title} className="border-t border-hairline py-6">
            <h3 className="text-title font-semibold text-fg">{title}</h3>
            <p className="mt-2 max-w-measure leading-relaxed text-fg-muted">{body}</p>
          </div>
        ))}
      </div>
    </Section>
  )
}
