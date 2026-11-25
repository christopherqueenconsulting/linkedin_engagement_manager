import type { ReactNode } from 'react'
import Icon from './Icon'

// The marketing page's product visuals (issue #1300).
//
// These are DOM-drawn diagrams of real LEM surfaces, not screenshots — no browser is reachable from
// the agent worktree that built this page, and inventing a screenshot is not an option. They carry
// the real vocabulary (post statuses, the approval step, suggestions you send yourself) and deliberately carry
// NO numbers: an engagement count in a mock is a fabricated number whether or not anyone reads it
// as one. The two figures that do appear are documented product defaults, labelled as defaults.
//
// Everything here is decorative to a screen reader — the surrounding copy already says what the
// panel shows — so the wrapper is labelled once and the innards are hidden.

// Status reads from the pack's tokens only: settled states are an outline in the primary, the one
// that waits on you is an outline in muted text. No status hue outside DESIGN.md (design pass,
// 86e3rnp9a). Outlined, not filled (design pass r2): the hero's trial button stays the only filled
// blue shape in the fold.
const STATUS_TONE: Record<string, string> = {
  Approved: 'border border-accent text-accent',
  'Pending review': 'border border-hairline text-fg-muted',
  Scheduled: 'border border-accent text-accent',
}

// Every panel is a drawn diagram with sample content, so each one carries a visible caption that
// says so (design pass r2), in the muted token at the 13px step. The title bar names the LEM
// surface; it no longer imitates a browser window with three dots.
function Panel({
  label,
  caption,
  children,
  fill = false,
}: {
  label: string
  /** Visible text under the panel that says it is an example with sample content. */
  caption: string
  children: ReactNode
  /** Stretch to the parent's height (the hero, where the frame runs to the trial line). */
  fill?: boolean
}) {
  return (
    <figure className={fill ? 'flex h-full flex-col' : undefined}>
      <div
        role="img"
        aria-label={label}
        className={`rounded-panel border border-hairline bg-raised shadow-panel overflow-hidden${fill ? ' flex flex-1 flex-col' : ''}`}
      >
        <div aria-hidden="true" className="border-b border-hairline bg-page px-4 py-3">
          <span className="text-xs font-medium text-fg-muted">Content Studio</span>
        </div>
        <div aria-hidden="true" className={fill ? 'flex flex-1 flex-col gap-6 p-4' : 'p-4 space-y-3'}>
          {children}
        </div>
      </div>
      <figcaption className="mt-2 text-xs text-fg-muted">{caption}</figcaption>
    </figure>
  )
}

function Row({
  title,
  status,
  archetype,
  stage,
}: {
  title: string
  status: keyof typeof STATUS_TONE
  archetype: string
  stage: string
}) {
  // Titles wrap rather than truncate: at 390px an ellipsis hid half of every title.
  return (
    <div className="flex items-start gap-3 rounded-lg border border-hairline px-3 py-3">
      <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg bg-page text-accent">
        <Icon name="pen" className="h-4 w-4" />
      </span>
      <span className="min-w-0 flex-1">
        <span className="block text-sm font-medium text-fg">{title}</span>
        <span className="mt-1 flex flex-wrap items-center gap-2 text-xs text-fg-muted">
          {archetype}
          <span className="rounded-full border border-hairline px-2 py-0.5">{stage}</span>
        </span>
      </span>
      <span className={`shrink-0 rounded-full px-2.5 py-1 text-xs font-semibold ${STATUS_TONE[status]}`}>
        {status}
      </span>
    </div>
  )
}

// A rendered row, not placeholder bars: grey bars read as a loading state, not as the product.
// The comment and reply are illustrative sample text for the diagram, and claim nothing about LEM.
function EngagementPane() {
  return (
    <>
      <div className="rounded-lg border border-hairline px-3 py-3">
        <div className="flex items-start justify-between gap-3">
          <span className="text-sm font-medium text-fg">Founder, B2B agency</span>
          <span className="shrink-0 rounded-full border border-hairline px-2 py-0.5 text-xs text-fg-muted">
            Review
          </span>
        </div>
        <p className="mt-1 text-xs text-fg-muted">How do you decide which ideas become a carousel?</p>
        <div className="mt-3 rounded-lg border border-hairline bg-page p-3">
          <div className="flex items-center gap-2 text-xs font-semibold text-accent">
            <Icon name="chat" className="h-4 w-4" />
            Suggested comment, in your voice
          </div>
          <p className="mt-2 text-sm text-fg">
            When the idea has steps. A single point reads better as a short text post.
          </p>
        </div>
      </div>
      <div className="flex items-center gap-2 text-xs text-fg-muted">
        <Icon name="check" className="h-4 w-4 text-accent" />
        You review it and send it yourself
      </div>
    </>
  )
}

// The engagement beat's own visual (design pass r2): it used to repeat the hero's comment card. It
// draws the beat's other two suggestion kinds, a reply on your own post and a follow-up DM, each
// waiting for you to send it. The quoted lines are sample text and claim nothing about LEM.
function Suggestion({
  icon,
  context,
  quote,
  kind,
  draft,
}: {
  icon: 'reply' | 'mail'
  context: string
  quote: string
  kind: string
  draft: string
}) {
  return (
    <div className="rounded-lg border border-hairline px-3 py-3">
      <div className="flex items-start justify-between gap-3">
        <span className="text-xs font-medium text-fg-muted">{context}</span>
        <span className="shrink-0 rounded-full border border-hairline px-2 py-0.5 text-xs text-fg-muted">
          Waiting for you
        </span>
      </div>
      <p className="mt-1 text-sm text-fg">{quote}</p>
      <div className="mt-3 border-l-2 border-accent pl-3">
        <div className="flex items-center gap-2 text-xs font-semibold text-accent">
          <Icon name={icon} className="h-4 w-4" />
          {kind}
        </div>
        <p className="mt-1 text-sm text-fg">{draft}</p>
      </div>
    </div>
  )
}

function FollowUpPane() {
  return (
    <>
      <Suggestion
        icon="reply"
        context="Comment on your post"
        quote="Do you share the checklist from this post?"
        kind="Suggested reply"
        draft="Happy to. It is a one-pager; I will send it over."
      />
      <Suggestion
        icon="mail"
        context="Follow-up after that comment"
        quote="Thanks, that would help."
        kind="Suggested follow-up DM"
        draft="Here is the checklist I mentioned. Thanks for reading."
      />
    </>
  )
}

function QueueRows() {
  return (
    <>
      <Row title="Why most LinkedIn advice fails consultants" status="Pending review" archetype="Thought leadership" stage="awareness" />
      <Row title="What we changed after last quarter" status="Approved" archetype="Personal story" stage="consideration" />
      <Row title="A 3-slide breakdown of the offer" status="Scheduled" archetype="Carousel" stage="decision" />
    </>
  )
}

export type PanelVariant = 'hero' | 'queue' | 'plan' | 'schedule' | 'engagement' | 'measure'

export default function ProductPanel({ variant }: { variant: PanelVariant }) {
  if (variant === 'schedule') {
    return (
      <Panel
        label="Diagram of the LEM scheduling calendar: a week of posting days with a draft placed in each golden-hour slot."
        caption="Example posting week, drawn for illustration"
      >
        <div className="grid grid-cols-5 gap-2">
          {['Mon', 'Tue', 'Wed', 'Thu', 'Fri'].map((day, index) => (
            <div key={day} className="rounded-lg border border-hairline p-2 text-center">
              <div className="text-xs font-semibold text-fg-muted">{day}</div>
              <div className={`mt-2 h-10 rounded ${index % 2 === 0 ? 'bg-accent' : 'bg-page'}`} />
            </div>
          ))}
        </div>
        <div className="flex items-center gap-2 rounded-lg bg-page px-3 py-2 text-xs text-fg">
          <Icon name="clock" className="h-4 w-4 text-accent" />
          Golden hour, in your timezone · 2 to 7 posts a week, 3 by default
        </div>
      </Panel>
    )
  }

  // The hero's one bold element: the approval queue and the suggestion you send yourself, side by
  // side in one frame, so the product (not a card) carries the fold.
  if (variant === 'hero') {
    return (
      <Panel
        fill
        label="Diagram of LEM: the approval queue with three drafts beside a suggested comment you edit and send yourself."
        caption="Example queue with sample posts and a sample comment"
      >
        {/* Stretched to the trial line, the extra height goes between the cards (equal gaps), and
            the right column's last line ends level with the left column's last card. */}
        <div className="grid flex-1 gap-4 xl:grid-cols-[3fr_2fr]">
          <div className="flex flex-col justify-between gap-3">
            <QueueRows />
          </div>
          <div className="flex flex-col gap-3">
            <EngagementPane />
          </div>
        </div>
        <div className="flex items-center gap-2 rounded-lg bg-page px-3 py-2 text-xs text-fg">
          <Icon name="shield" className="h-4 w-4 text-accent" />
          Posts publish only after you approve them
        </div>
      </Panel>
    )
  }

  // The content beat's own visual (design pass 2026-11-24): it used to repeat the hero's queue row
  // for row. It draws the two facts the beat states, the buyer-journey stages and the 70/20/10
  // value / authority / promo mix, as a sequence and a share bar. No count of posts is drawn.
  if (variant === 'plan') {
    return (
      <Panel
        label="Diagram of a LEM 30-day plan: drafts placed across the awareness, consideration and decision stages, in a 70/20/10 value, authority and promo mix."
        caption="Example plan layout, drawn for illustration"
      >
        <div className="text-xs font-semibold text-fg-muted">30-day plan, by buyer stage</div>
        {/* A row from sm; on a phone the stages stack, so "Consideration" never truncates. */}
        <div className="flex flex-col items-stretch gap-1 sm:flex-row sm:items-center sm:gap-2">
          {['Awareness', 'Consideration', 'Decision'].flatMap((stage, index) => [
            ...(index > 0
              ? [
                  <Icon
                    key={`${stage}-arrow`}
                    name="arrowRight"
                    className="h-4 w-4 shrink-0 rotate-90 self-center text-fg-muted sm:rotate-0"
                  />,
                ]
              : []),
            <span
              key={stage}
              className="min-w-0 flex-1 rounded-lg border border-hairline px-2 py-2 text-center text-sm font-medium text-fg"
            >
              {stage}
            </span>,
          ])}
        </div>
        <div className="pt-2 text-xs font-semibold text-fg-muted">Content mix</div>
        <div className="flex h-3 overflow-hidden rounded-full">
          <span className="w-[70%] bg-accent" />
          <span className="w-[20%] bg-fg-muted" />
          <span className="w-[10%] bg-field" />
        </div>
        <div className="flex flex-wrap gap-x-5 gap-y-2 text-sm text-fg">
          {[
            ['bg-accent', 'Value 70%'],
            ['bg-fg-muted', 'Authority 20%'],
            ['bg-field', 'Promo 10%'],
          ].map(([swatch, label]) => (
            <span key={label} className="flex items-center gap-2">
              <span className={`h-2.5 w-2.5 rounded-full ${swatch}`} />
              {label}
            </span>
          ))}
        </div>
      </Panel>
    )
  }

  if (variant === 'engagement') {
    return (
      <Panel
        label="Diagram of LEM engagement suggestions: a suggested reply to a comment on your post and a suggested follow-up DM, each waiting for you to send it."
        caption="Example suggestions with sample text"
      >
        <FollowUpPane />
      </Panel>
    )
  }

  if (variant === 'measure') {
    return (
      <Panel
        label="Diagram of the LEM measurement view: post and audience statistics, each waiting on real data."
        caption="Example layout, shown without data"
      >
        {['Post statistics', 'Audience statistics'].map((row) => (
          <div
            key={row}
            className="flex items-center justify-between rounded-lg border border-hairline px-3 py-3"
          >
            <span className="text-sm text-fg">{row}</span>
            <span className="text-sm font-semibold text-fg-muted">from your account</span>
          </div>
        ))}
      </Panel>
    )
  }

  return (
    <Panel
      label="Diagram of the LEM approval queue: three drafts, one approved, one waiting for review and one scheduled."
      caption="Example queue with sample posts"
    >
      <QueueRows />
      <div className="flex items-center gap-2 rounded-lg bg-page px-3 py-2 text-xs text-fg">
        <Icon name="shield" className="h-4 w-4 text-accent" />
        Posts publish only after you approve them
      </div>
    </Panel>
  )
}
