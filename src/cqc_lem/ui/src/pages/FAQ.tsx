import { useEffect, useState } from 'react'
import api from '../api/client'

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
    answer:
      'Every plan starts with a 14-day free trial — no credit card required. After the trial, Starter is $29/month, Professional is $79/month and Enterprise is $199/month. The trial gives you the full Professional feature set so you can evaluate content generation, scheduling and engagement automation before you pay.',
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
  return (
    <div className="border border-gray-200 rounded-lg overflow-hidden">
      <button
        type="button"
        onClick={() => setOpen(!open)}
        aria-expanded={open}
        className="w-full flex justify-between items-center px-5 py-4 text-left font-medium text-gray-800 hover:bg-gray-50 transition-colors"
      >
        <span>{question}</span>
        <span className="text-gray-400 text-lg leading-none">{open ? '−' : '+'}</span>
      </button>
      {open && (
        <div className="px-5 pb-4 text-sm text-gray-600 leading-relaxed">
          {answer}
        </div>
      )}
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
    <section id="faq" className="py-20 px-4 bg-white">
      <div className="max-w-3xl mx-auto">
        <h2 className="text-3xl font-bold text-center text-gray-800 mb-12">Frequently Asked Questions</h2>
        <div className="space-y-3">
          {entries.map((entry) => (
            <FaqItem key={entry.id} question={entry.question} answer={entry.answer} />
          ))}
        </div>
      </div>
    </section>
  )
}
