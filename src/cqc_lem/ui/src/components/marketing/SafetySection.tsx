import Icon from './Icon'
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
const CLAIMS: { title: string; body: string }[] = [
  {
    title: 'LinkedIn sign-in, never your password',
    body: 'LEM never asks for your LinkedIn password or a session cookie.',
  },
  {
    title: 'No post goes out without your approval',
    body: 'Each post waits; only after you approve it is it published, through LinkedIn’s API.',
  },
  {
    title: 'Engagement stays in your hands',
    body: 'Comments, replies and DMs are suggestions; LEM does not send them for you.',
  },
  {
    title: 'Quality gates before you see a draft',
    body: 'A draft that fails the quality contract is held, with the reason shown.',
  },
  {
    title: 'Secrets encrypted at rest',
    body: 'Sign-in tokens are AES-256-GCM encrypted per user, with passkey or TOTP step-up on the writes that touch them.',
  },
]

// Design pass 2026-11-24: a two-column ledger rather than five boxed icon cards. The mechanisms
// read as one ruled list, title then detail, beside the heading. The limit stays LAST in the DOM
// (so a phone and a screen reader still reach it after the list); from lg it is placed under the
// heading in the left column.
export default function SafetySection() {
  return (
    <Section id="safety" analyticsName="safety" tone="dark">
      <div className="grid gap-y-10 lg:grid-cols-12 lg:grid-rows-[auto_1fr] lg:gap-x-14">
        <div className="min-w-0 lg:col-span-5">
          <SectionHeading title="Nothing goes out under your name until you say so" />
        </div>
        <ul className="min-w-0 border-t border-hairline lg:col-span-7 lg:row-span-2">
          {CLAIMS.map(({ title, body }) => (
            <li key={title} className="max-w-none border-b border-hairline py-5">
              <span className="block font-semibold text-fg">{title}</span>
              <span className="mt-1 block leading-relaxed text-fg-muted">{body}</span>
            </li>
          ))}
        </ul>
        <p className="flex min-w-0 items-start gap-3 self-start rounded-panel border border-field p-5 text-sm leading-relaxed text-fg lg:col-span-5 lg:col-start-1 lg:row-start-2 lg:-mt-12">
          <span className="shrink-0">
            <Icon name="shield" className="h-5 w-5" />
          </span>
          <span>
            <strong className="font-semibold">And the limit:</strong> no tool can guarantee a LinkedIn
            account. You can edit any draft, approve nothing or send no suggestion, and you remain
            responsible for your own account.
          </span>
        </p>
      </div>
    </Section>
  )
}
