import { useEffect, useState } from 'react'
import api from '../api/client'
import { FLAGS, useFeatureFlag } from '../hooks/useFeatureFlags'

// Front-page "Made with LEM" showcase (issue #1299). Renders real posts published by the LEM
// brand account with their stored engagement counts. Gated by the same `brand-showcase-enabled`
// flag that gates the public endpoint, so one runtime toggle covers both the API and the section.
// Nothing to show -> renders nothing, matching the TutorialVideos pattern.
interface BrandPost {
  id: number
  content: string
  post_type: string
  published_at: string | null
  post_url: string | null
  reactions: number | null
  comments: number | null
  reposts: number | null
  impressions: number | null
  saves: number | null
}

interface BrandShowcasePayload {
  posts: BrandPost[]
}

function formatStoredNumber(value: number | null): string | null {
  if (value === null || value === undefined || !Number.isFinite(value)) return null
  return value.toLocaleString()
}

function truncateContent(text: string, maxChars = 180): string {
  if (text.length <= maxChars) return text
  const truncated = text.slice(0, maxChars)
  const lastSpace = truncated.lastIndexOf(' ')
  return `${truncated.slice(0, lastSpace > 0 ? lastSpace : maxChars)}…`
}

export default function BrandShowcase() {
  const [posts, setPosts] = useState<BrandPost[]>([])
  const enabled = useFeatureFlag(FLAGS.brandShowcase)

  useEffect(() => {
    if (!enabled) return
    let cancelled = false
    api
      .get('/brand-showcase')
      .then((r) => {
        const payload: BrandShowcasePayload = r.data?.detail ?? { posts: [] }
        const usable = (payload.posts ?? []).filter(
          (p): p is BrandPost =>
            p != null && typeof p.id === 'number' && typeof p.content === 'string',
        )
        if (!cancelled) setPosts(usable)
      })
      .catch(() => undefined)
    return () => {
      cancelled = true
    }
  }, [enabled])

  if (!enabled || posts.length === 0) return null

  return (
    <section id="brand-showcase" className="py-16 px-4 bg-surface-50">
      <div className="max-w-5xl mx-auto">
        <h2 className="text-headline sm:text-display font-bold text-center text-ink-900 mb-3">
          Made with LEM
        </h2>
        <p className="text-center text-ink-600 mb-12 max-w-2xl mx-auto">
          Real posts written and scheduled by LEM, running on our own brand account.
        </p>
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-6">
          {posts.map((post) => (
            <article
              key={post.id}
              className="bg-white rounded-card border border-line-200 shadow-card p-5 flex flex-col"
            >
              <p className="text-ink-900 text-sm leading-relaxed flex-1 whitespace-pre-line">
                {truncateContent(post.content)}
              </p>
              <div className="mt-4 pt-4 border-t border-line-200 grid grid-cols-3 gap-2 text-center">
                <div>
                  <div className="text-lg font-bold text-brand-600">
                    {formatStoredNumber(post.reactions) ?? '—'}
                  </div>
                  <div className="text-xs text-ink-500">reactions</div>
                </div>
                <div>
                  <div className="text-lg font-bold text-brand-600">
                    {formatStoredNumber(post.comments) ?? '—'}
                  </div>
                  <div className="text-xs text-ink-500">comments</div>
                </div>
                <div>
                  <div className="text-lg font-bold text-brand-600">
                    {formatStoredNumber(post.reposts) ?? '—'}
                  </div>
                  <div className="text-xs text-ink-500">reposts</div>
                </div>
              </div>
              {post.post_url && (
                <a
                  href={post.post_url}
                  target="_blank"
                  rel="noreferrer"
                  className="mt-4 text-sm font-medium text-brand-600 hover:text-brand-700 hover:underline"
                >
                  View on LinkedIn →
                </a>
              )}
            </article>
          ))}
        </div>
      </div>
    </section>
  )
}
