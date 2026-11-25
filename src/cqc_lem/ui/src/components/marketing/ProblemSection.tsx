import Section, { SectionHeading } from './Section'

// The named-problem section (issue #1300) — the most consistent section in this category and the
// one the old page had no version of. It goes between the proof strip and how-it-works: a visitor
// who does not recognise their own problem has no reason to read a feature list.
// Design pass r3: the four statements stand alone, with no line under each (density budget). The
// cut lines restated the statement above them; the engagement half and the automation worry are
// answered in full by the feature beats and the safety ledger.
const PROBLEMS: string[] = [
  'You post in bursts, then vanish',
  'Publishing is the easy half',
  'Generic AI sounds like generic AI',
  'And automation makes you nervous',
]

export default function ProblemSection() {
  return (
    <Section id="problem" analyticsName="problem" tone="white">
      <SectionHeading
        title="Showing up on LinkedIn is a daily job nobody has time for"
        lede="The same problem, twice a day, every weekday."
      />
      {/* Plain statements on hairline rules, matching the proof strip, not a grid of icon cards. */}
      <div className="grid gap-x-12 md:grid-cols-2">
        {PROBLEMS.map((title) => (
          <div key={title} className="border-t border-hairline py-6">
            <h3 className="text-title font-semibold text-fg">{title}</h3>
          </div>
        ))}
      </div>
    </Section>
  )
}
