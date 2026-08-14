import Icon, { type IconName } from './Icon'
import Section from './Section'

// What replaced the fabricated stat strip (issue #1300).
//
// The page used to open with "2,500+ Active Users", "50K+ Posts Automated" and "85% Avg Engagement
// Boost" — none of them sourced, and all three contradicted by `docs/launch-and-marketing-plan.md`,
// which caps the current launch phase at 25 users. Every line here is MECHANISM proof: a thing the
// product does, verifiable by using it and traceable to a file. No counts, because we do not have
// counts worth showing yet, and saying so is cheaper than being caught.
const PROOF: { icon: IconName; title: string; body: string }[] = [
  {
    icon: 'shield',
    title: 'Approval before anything ships',
    body: 'Posts preview before they publish and generated DMs land as drafts.',
  },
  {
    icon: 'gauge',
    title: 'Caps you set, not caps we chose',
    body: 'Comments, DMs and invites each stop at your own per-day number.',
  },
  {
    icon: 'clock',
    title: 'One pacing engine, not a loop',
    body: 'Every delay is drawn per user, action and day, and persisted so a retry never re-rolls it.',
  },
  {
    icon: 'lock',
    title: 'Credentials encrypted at rest',
    body: 'AES-256-GCM per user and column, with passkey or TOTP step-up on the writes that matter.',
  },
]

export default function ProofStrip() {
  return (
    <Section analyticsName="proof" tone="tinted" className="py-12 sm:py-14">
      <p className="text-center text-sm font-semibold uppercase tracking-wide text-brand-600">
        Early access — we are onboarding a small first cohort
      </p>
      <p className="mx-auto mt-3 max-w-2xl text-center text-ink-600">
        No user-count banner here yet. What we can show you is what the product actually does.
      </p>
      <ul className="mt-10 grid gap-6 sm:grid-cols-2 lg:grid-cols-4">
        {PROOF.map(({ icon, title, body }) => (
          <li key={title} className="flex gap-3">
            <span className="mt-0.5 shrink-0 text-brand-600">
              <Icon name={icon} className="h-6 w-6" />
            </span>
            <span>
              <span className="block font-semibold text-ink-900">{title}</span>
              <span className="mt-1 block text-sm leading-relaxed text-ink-600">{body}</span>
            </span>
          </li>
        ))}
      </ul>
    </Section>
  )
}
