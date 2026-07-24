import { useState } from 'react'

interface LinkedInPostPreviewProps {
  content: string
  author?: string
  headline?: string
  mediaUrl?: string
  videoUrl?: string | null
  slides?: string[] | null
  postType?: string
}

function isUrl(value: string): boolean {
  return /^(https?:)?\/\//.test(value) || value.startsWith('/api/') || value.startsWith('/assets')
}

// Only allow safe schemes to reach a media `src` — blocks javascript:/vbscript:/etc.
// so post-derived URLs can't become a DOM-based XSS sink. Validates via the URL
// parser and returns the normalized href (parse + scheme allowlist sanitizes the value).
const SAFE_MEDIA_PROTOCOLS = new Set(['http:', 'https:', 'blob:', 'data:'])

function safeMediaUrl(value: string | null | undefined): string | undefined {
  if (!value) return undefined
  const origin = typeof window !== 'undefined' ? window.location.origin : 'http://localhost'
  try {
    const parsed = new URL(value, origin)
    if (SAFE_MEDIA_PROTOCOLS.has(parsed.protocol)) {
      return parsed.href
    }
  } catch {
    // not a parseable URL — fall through to undefined
  }
  return undefined
}

function CarouselPreview({ slides }: { slides: string[] }) {
  const [index, setIndex] = useState(0)
  const total = slides.length
  const current = slides[index]
  const go = (delta: number) => setIndex((i) => (i + delta + total) % total)

  return (
    <div className="relative bg-gray-900 select-none">
      {/* LinkedIn renders document/carousel posts as a square page viewer */}
      <div className="aspect-square w-full flex items-center justify-center overflow-hidden">
        {isUrl(current) ? (
          <img
            src={safeMediaUrl(current)}
            alt={`Slide ${index + 1} of ${total}`}
            className="w-full h-full object-contain"
          />
        ) : (
          <div className="w-full h-full flex items-center justify-center p-8 text-center bg-gradient-to-br from-blue-700 to-blue-900">
            <p className="text-white text-lg font-semibold leading-snug whitespace-pre-wrap">{current}</p>
          </div>
        )}
      </div>

      {total > 1 && (
        <>
          <button
            type="button"
            onClick={(e) => { e.stopPropagation(); go(-1) }}
            aria-label="Previous slide"
            className="absolute left-2 top-1/2 -translate-y-1/2 w-8 h-8 rounded-full bg-white/90 text-gray-800 shadow flex items-center justify-center hover:bg-white"
          >
            ‹
          </button>
          <button
            type="button"
            onClick={(e) => { e.stopPropagation(); go(1) }}
            aria-label="Next slide"
            className="absolute right-2 top-1/2 -translate-y-1/2 w-8 h-8 rounded-full bg-white/90 text-gray-800 shadow flex items-center justify-center hover:bg-white"
          >
            ›
          </button>
        </>
      )}

      {/* Page counter badge, like LinkedIn document posts */}
      <div className="absolute bottom-2 right-2 bg-black/60 text-white text-xs font-medium px-2 py-0.5 rounded">
        {index + 1} / {total}
      </div>

      {/* Dot indicators */}
      {total > 1 && total <= 12 && (
        <div className="absolute bottom-2 left-1/2 -translate-x-1/2 flex gap-1.5">
          {slides.map((_, i) => (
            <button
              key={i}
              type="button"
              onClick={(e) => { e.stopPropagation(); setIndex(i) }}
              aria-label={`Go to slide ${i + 1}`}
              className={`w-1.5 h-1.5 rounded-full transition-colors ${i === index ? 'bg-white' : 'bg-white/40'}`}
            />
          ))}
        </div>
      )}
    </div>
  )
}

export default function LinkedInPostPreview({
  content,
  author = 'Your Name',
  headline = 'Your Headline',
  mediaUrl,
  videoUrl,
  slides,
  postType,
}: LinkedInPostPreviewProps) {
  const lines = content.split('\n')
  const preview = lines.slice(0, 3).join('\n')
  const hasMore = lines.length > 3 || content.length > 300

  const showVideo = (postType === 'video' || !!videoUrl) && !!videoUrl
  // A native document post is the same slide deck as a carousel, published as one PDF.
  const showCarousel = !showVideo && (postType === 'carousel' || postType === 'document')
    && !!slides && slides.length > 0
  const showImage = !showVideo && !showCarousel && !!mediaUrl

  return (
    <div className="bg-white rounded-lg border border-gray-200 shadow-sm max-w-lg font-sans">
      {/* Header */}
      <div className="flex items-center gap-3 p-4">
        <div className="w-12 h-12 rounded-full bg-blue-600 flex items-center justify-center text-white font-bold text-lg flex-shrink-0">
          {author.charAt(0).toUpperCase()}
        </div>
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-1">
            <span className="font-semibold text-gray-900 text-sm truncate">{author}</span>
            <span className="text-gray-400 text-xs">• 1st</span>
          </div>
          <p className="text-xs text-gray-500 truncate">{headline}</p>
          <div className="flex items-center gap-1 text-xs text-gray-400 mt-0.5">
            <span>Just now</span>
            <span>·</span>
            <svg className="w-3 h-3" viewBox="0 0 16 16" fill="currentColor">
              <path d="M8 0C3.58 0 0 3.58 0 8s3.58 8 8 8 8-3.58 8-8-3.58-8-8-8zM7 11.5L3.5 8l1.41-1.41L7 8.67l4.59-4.58L13 5.5 7 11.5z"/>
            </svg>
          </div>
        </div>
        <button className="text-blue-600 border border-blue-600 rounded-full px-3 py-1 text-xs font-semibold hover:bg-blue-50">
          + Follow
        </button>
      </div>

      {/* Content */}
      <div className="px-4 pb-3">
        <p className="text-sm text-gray-800 whitespace-pre-wrap leading-relaxed">
          {hasMore ? preview : content}
          {hasMore && (
            <span className="text-gray-500 cursor-pointer"> …<span className="text-blue-600"> see more</span></span>
          )}
        </p>
      </div>

      {/* Media */}
      {showVideo && (
        <div className="bg-black">
          <video
            key={videoUrl ?? undefined}
            src={safeMediaUrl(videoUrl)}
            controls
            playsInline
            preload="metadata"
            className="w-full max-h-[32rem] object-contain bg-black"
          />
        </div>
      )}
      {showCarousel && <CarouselPreview slides={slides!} />}
      {showImage && (
        <div className="bg-gray-100 aspect-video overflow-hidden">
          <img src={safeMediaUrl(mediaUrl)} alt="Post media" className="w-full h-full object-cover" />
        </div>
      )}

      {/* Stats bar */}
      <div className="px-4 py-2 flex items-center justify-between text-xs text-gray-500 border-t border-gray-100">
        <div className="flex items-center gap-1">
          <span className="text-blue-600">👍</span>
          <span>Be the first to react</span>
        </div>
        <span>0 comments</span>
      </div>

      {/* Action buttons */}
      <div className="flex items-center border-t border-gray-100">
        {['👍 Like', '💬 Comment', '🔁 Repost', '📤 Send'].map((action) => (
          <button
            key={action}
            className="flex-1 py-3 text-xs font-semibold text-gray-500 hover:bg-gray-50 flex items-center justify-center gap-1"
          >
            {action}
          </button>
        ))}
      </div>
    </div>
  )
}
