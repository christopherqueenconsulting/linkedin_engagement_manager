import Icon from './Icon'
import Section from './Section'

// What replaced the fabricated stat strip (issue #1300).
//
// The page used to open with "2,500+ Active Users", "50K+ Posts Automated" and "85% Avg Engagement
// Boost" — none of them sourced, and all three contradicted by `docs/launch-and-marketing-plan.md`,
// which caps the current launch phase at 25 users. Every line here is MECHANISM proof: a thing the
// product does, verifiable by using it and traceable to a file. No counts, because we do not have
// counts worth showing yet, and saying so is cheaper than being caught.
//
// Design pass 2026-11-24: the strip is the page's summary of the safety posture, so it carries the
// four statements alone. Each one's detail is stated in full in the safety section below, and
// repeating those sentences here put the same claim on the page twice.
const PROOF: string[] = [
  'Posts wait for your approval',
  'Engagement you send yourself',
  'LinkedIn sign-in only',
  'Credentials encrypted at rest',
]

export default function ProofStrip() {
  return (
    <Section analyticsName="proof" tone="tinted" className="py-10 sm:py-12">
      <p className="text-sm font-semibold text-fg-muted">
        Early access — we are onboarding a small first cohort
      </p>
      {/* One quiet row of plain statements with a drawn check, not a grid of cards. */}
      <ul className="mt-5 grid gap-x-8 gap-y-3 border-t border-hairline pt-5 sm:grid-cols-2 lg:flex lg:justify-between">
        {PROOF.map((title) => (
          <li key={title} className="flex items-start gap-3 text-base font-semibold text-fg">
            <Icon name="check" className="mt-0.5 h-5 w-5 shrink-0 text-accent" />
            {title}
          </li>
        ))}
      </ul>
    </Section>
  )
}
