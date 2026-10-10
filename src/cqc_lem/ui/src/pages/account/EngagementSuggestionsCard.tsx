import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import api from '../../api/client'
import { useAuth } from '../../contexts/useAuth'
import type { GetDetail } from '../../api/types'
import { maskProps } from '../../utils/analytics'

// The Suggested list (issue #2367).
//
// A suggest-only account (every new trial) runs no browser automation on LinkedIn. The comment and
// DM drafts its engagement lanes would have posted are stored instead, and this card is where the
// user picks them up: read, copy, paste on LinkedIn themselves. An `automate` account with nothing
// stored sees nothing here — the card only appears when it has something to say.

type SuggestionsResponse = GetDetail<'/api/user/engagement-suggestions'>
type Suggestion = SuggestionsResponse['suggestions'][number]

const KIND_LABEL: Record<Suggestion['kind'], string> = {
  comment: 'Comment',
  reply: 'Reply',
  dm: 'Direct message',
}

function when(iso: string | null): string | null {
  if (!iso) return null
  const d = new Date(iso)
  return Number.isNaN(d.getTime()) ? null : d.toLocaleString()
}

function SuggestionRow({ item }: { item: Suggestion }) {
  const [copied, setCopied] = useState<'ok' | 'failed' | null>(null)

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(item.body)
      setCopied('ok')
      setTimeout(() => setCopied(null), 2500)
    } catch {
      // A browser that refuses clipboard access (insecure origin, denied permission) — say so,
      // so the user selects the text by hand instead of pasting whatever was there before.
      setCopied('failed')
    }
  }

  const created = when(item.created_at)

  return (
    <li data-testid="engagement-suggestion" className="border border-gray-200 rounded-lg p-4 space-y-2">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex items-center gap-2 text-xs text-gray-500">
          <span className="px-2 py-0.5 rounded-full bg-blue-50 text-blue-700 font-medium">
            {KIND_LABEL[item.kind] ?? item.kind}
          </span>
          {created && <span>{created}</span>}
        </div>
        <button
          type="button"
          onClick={copy}
          className="px-3 py-1 text-xs font-medium rounded-md border border-gray-300 text-gray-700 hover:bg-gray-50"
        >
          {copied === 'ok' ? 'Copied' : 'Copy'}
        </button>
      </div>
      <p {...maskProps('text-sm text-gray-800 whitespace-pre-wrap')}>{item.body}</p>
      {item.target_url && (
        <a
          href={item.target_url}
          target="_blank"
          rel="noopener noreferrer"
          className="text-xs text-blue-600 hover:underline break-all"
        >
          Open on LinkedIn
        </a>
      )}
      {copied === 'failed' && (
        <p className="text-xs text-red-600">Could not copy — select the text and copy it by hand.</p>
      )}
    </li>
  )
}

export default function EngagementSuggestionsCard() {
  const { sessionToken } = useAuth()

  const { data, isError } = useQuery({
    queryKey: ['engagement-suggestions', sessionToken],
    queryFn: () =>
      api
        .get(`/user/engagement-suggestions?session_token=${encodeURIComponent(sessionToken!)}`)
        .then((r) => r.data.detail as SuggestionsResponse),
    enabled: !!sessionToken,
    staleTime: 60 * 1000,
  })

  if (isError) {
    return (
      <div data-testid="engagement-suggestions" className="bg-white rounded-lg shadow-sm border border-gray-200 p-6">
        <h2 className="text-base font-semibold text-gray-700">Suggested</h2>
        <p className="text-sm text-gray-500 mt-2">Suggestions are temporarily unavailable.</p>
      </div>
    )
  }
  if (!data) return null

  const suggestOnly = data.engagement_mode === 'suggest'
  if (!suggestOnly && data.suggestions.length === 0) return null

  return (
    <div data-testid="engagement-suggestions" className="bg-white rounded-lg shadow-sm border border-gray-200 p-6 space-y-4">
      <div>
        <h2 className="text-base font-semibold text-gray-700">Suggested</h2>
        {suggestOnly && (
          <p className="text-xs text-gray-500 mt-1">
            Your account gets engagement as suggestions: LEM never signs in to LinkedIn or posts
            comments and messages for you. Copy a draft and post it yourself.
          </p>
        )}
      </div>
      {data.suggestions.length === 0 ? (
        <p className="text-sm text-gray-500">
          No suggestions yet. Drafts appear here as LEM writes them — for example, a first comment
          for each post you publish.
        </p>
      ) : (
        <ul className="space-y-3">
          {data.suggestions.map((item) => (
            <SuggestionRow key={item.id} item={item} />
          ))}
        </ul>
      )}
    </div>
  )
}
