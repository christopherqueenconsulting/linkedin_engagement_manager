import Icon, { type IconName } from './Icon'
import Section, { SectionHeading } from './Section'

// The page's differentiator (issue #1300).
//
// Account safety is this category's real battleground and most of the field either says nothing or
// says "smart limits and human-like timing". Specificity is the open lane — every claim below names
// a mechanism that exists in this repo, and the PR that shipped this page carries the ledger
// mapping each one to its file. A claim with no owner is deleted, not softened.
//
// The ceiling on how strongly the terms-of-service question may be answered is the owner-approved
// FAQ answer (`pages/FAQ.tsx`, fallback entry -3). Nothing here goes past it, and the closing
// caveat is deliberately the last thing in the section.
const CLAIMS: { icon: IconName; title: string; body: string }[] = [
  {
    icon: 'clock',
    title: 'One cadence engine, and it does not re-roll',
    body: 'Every delay is drawn from a single pacing engine, seeded on the user, the action and the day, then persisted — so a retry replays the same timing instead of rolling a fresh burst of activity.',
  },
  {
    icon: 'gauge',
    title: 'Per-day caps that are yours',
    body: 'Comments, DMs and invites each stop at a number you set. Nothing raises them for you, and no campaign borrows tomorrow’s allowance.',
  },
  {
    icon: 'shield',
    title: 'A rate-limit breaker that is not a toggle',
    body: 'When LinkedIn answers 429 or shows an auth wall, activity stops. That breaker is deliberately not a feature flag — safety controls here are not the kind of thing anyone gets to switch off in a hurry.',
  },
  {
    icon: 'lock',
    title: 'Your session, egress matched to your location',
    body: 'Automation runs from your own logged-in session through an egress proxy matched to your country, not a raw shared datacenter pool — a dedicated per-user proxy is available as a paid upgrade.',
  },
  {
    icon: 'chat',
    title: 'Approval gates, as a safety mechanism',
    body: 'Generated DMs land as drafts and posts preview before they publish. A human shipping it is the cheapest protection there is, and no competitor frames it as one.',
  },
  {
    icon: 'pen',
    title: 'Quality gates before anything is sent',
    body: 'Comments clear a quality contract and a similarity gate; five hard slop-lint checks block a draft outright. Nothing generic goes out under your name.',
  },
  {
    icon: 'calendar',
    title: 'A tripwire that pauses engagement, never posting',
    body: 'Reach is compared against your OWN trailing 14-day median, because the penalties this guards against are silent. A sustained drop pauses engagement and leaves publishing alone.',
  },
  {
    icon: 'lock',
    title: 'Secrets encrypted at rest',
    body: 'Credentials and session cookies are AES-256-GCM encrypted per user and column, and passkey or TOTP step-up gates the writes that touch them.',
  },
]

export default function SafetySection() {
  return (
    <Section id="safety" analyticsName="safety" tone="dark">
      <SectionHeading
        onDark
        eyebrow="Account safety"
        title="Built to act like you, and to stop before you would"
        lede="Most tools in this category answer this with one sentence. Here is the whole mechanism list, each one a thing you can hold us to."
      />
      <ul className="grid gap-6 md:grid-cols-2">
        {CLAIMS.map(({ icon, title, body }) => (
          <li key={title} className="flex gap-4 rounded-card border border-ink-700 p-5">
            <span className="mt-0.5 shrink-0 text-brand-300">
              <Icon name={icon} className="h-6 w-6" />
            </span>
            <span>
              <span className="block font-semibold text-white">{title}</span>
              <span className="mt-2 block text-sm leading-relaxed text-brand-300">{body}</span>
            </span>
          </li>
        ))}
      </ul>
      <p className="mt-10 flex items-start gap-3 rounded-card border border-warning-300 p-5 text-sm leading-relaxed text-warning-300">
        <span className="shrink-0">
          <Icon name="shield" className="h-5 w-5" />
        </span>
        <span>
          <strong className="font-semibold">And the limit:</strong> no tool can guarantee a LinkedIn
          account, and anyone who tells you otherwise is selling. What you control here is the whole
          surface — pause it, lower the caps, or approve nothing. You remain responsible for your own
          account.
        </span>
      </p>
    </Section>
  )
}
