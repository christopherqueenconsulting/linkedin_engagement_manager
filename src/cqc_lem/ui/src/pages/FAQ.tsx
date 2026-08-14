import { useEffect, useId, useState } from 'react'
import api from '../api/client'
import { TRIAL_DAYS, paidPricingSentence } from '../constants/plans'

// Front-page FAQ (issue #506). Entries come from `faq_entries` via the public GET /api/faq, which
// the auto-FAQ pass keeps current from the questions users actually ask. The seeded copy below is
// the fallback ONLY — if the API is unreachable the landing page still answers the basics rather
// than showing an empty section.
interface FaqEntry {
  id: number
  question: string
  answer: string
  updated_at?: string | null
}

const FALLBACK_ENTRIES: FaqEntry[] = [
  {
    id: -1,
    question: 'What does the automation actually do?',
    answer:
      'LEM writes and schedules your LinkedIn content, then engages for you: it comments on relevant posts in your feed, replies to comments on your own posts, seeds a first comment under what you publish, and sends appreciation and follow-up DMs. Every action follows the targeting rules, tone and per-day caps you set, and posts go through a preview/approval step before anything is published.',
  },
  {
    id: -2,
    question: 'How much does it cost, and is there a free trial?',
    // Built from the shared PLANS constant (issue #1300) rather than typed out again — this answer
    // and the pricing section used to be two independent copies of the same three numbers.
    answer:
      `Every plan starts with a ${TRIAL_DAYS}-day free trial — no credit card required. ` +
      `${paidPricingSentence()} ` +
      'The trial gives you the full product so you can evaluate content generation, scheduling and engagement automation before you pay.',
  },
  {
    id: -3,
    question: "Is this against LinkedIn's Terms of Service?",
    answer:
      "LinkedIn's User Agreement restricts automated tools, so LEM is built to act like you, not like a bot: it works from your own logged-in session, keeps human-like pacing with per-day caps, and never mass-messages, scrapes profiles in bulk, or resells LinkedIn data. Content is approval-gated by default, so you decide what gets posted. You are responsible for your own account, and you can pause automation or reduce caps at any time.",
  },
  {
    id: -4,
    question: 'What happens to my data and my LinkedIn credentials?',
    answer:
      "Your credentials and session cookies are stored encrypted and used only to run the automation you configured — we never sell or share your data, and we don't use your content to train public models. Generated posts, comments and DMs stay in your account, and deleting your account removes them.",
  },
  {
    id: -5,
    question: 'How does the AI match my voice?',
    answer:
      'LEM builds a voice profile from your existing LinkedIn posts and profile plus the tone, style, emoji and hashtag preferences you set on the Account page. Every post, comment and DM is generated against that profile and your focus topics, and you can edit or reject anything in the preview step — those edits feed back into how future drafts read.',
  },
  {
    id: -6,
    question: 'Can I cancel anytime?',
    answer:
      'Yes. There are no long-term contracts and no cancellation fees — cancel whenever you like and your account stays active through the end of the current billing period. You can also pause the automation without cancelling if you just need a break.',
  },
]

function FaqItem({ question, answer }: { question: string; answer: string }) {
  const [open, setOpen] = useState(false)
  // The answer needs a stable id so the disclosure button can point `aria-controls` at it —
  // without that pairing a screen reader hears "expanded" with nothing to say what expanded.
  const panelId = useId()

  return (
    <div className="rounded-card border border-line-200 bg-white overflow-hidden">
      <h3>
        <button
          type="button"
          onClick={() => setOpen(!open)}
          aria-expanded={open}
          aria-controls={panelId}
          className="w-full min-h-11 flex justify-between items-center gap-4 px-5 py-4 text-left font-medium text-ink-900 hover:bg-surface-50 transition-colors focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand-600"
        >
          <span>{question}</span>
          <span aria-hidden="true" className="text-brand-600 text-lg leading-none">
            {open ? '−' : '+'}
          </span>
        </button>
      </h3>
      <div id={panelId} hidden={!open} className="px-5 pb-4 text-sm text-ink-600 leading-relaxed">
        {answer}
      </div>
    </div>
  )
}

export default function FAQ() {
  const [entries, setEntries] = useState<FaqEntry[]>(FALLBACK_ENTRIES)

  useEffect(() => {
    let cancelled = false
    api
      .get('/faq')
      .then((r) => {
        const published: FaqEntry[] = r.data?.detail?.entries ?? []
        const usable = published.filter((e) => e?.question && e?.answer)
        if (!cancelled && usable.length > 0) setEntries(usable)
      })
      // Unreachable API on a public page is not worth an error state — keep the seeded copy.
      .catch(() => undefined)
    return () => {
      cancelled = true
    }
  }, [])

  return (
    <section id="faq" className="py-16 sm:py-20 px-4 bg-surface-50">
      <div className="max-w-3xl mx-auto">
        <h2 className="text-headline sm:text-display font-bold text-center text-ink-900 mb-12">
          Frequently asked questions
        </h2>
        <div className="space-y-3">
          {entries.map((entry) => (
            <FaqItem key={entry.id} question={entry.question} answer={entry.answer} />
          ))}
        </div>
      </div>
    </section>
  )
}
