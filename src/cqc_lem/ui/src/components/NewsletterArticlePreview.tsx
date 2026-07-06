interface NewsletterArticlePreviewProps {
  title?: string | null
  subtitle?: string | null
  body?: string | null
  author?: string
}

// A lightweight LinkedIn-newsletter-article mock: byline, title, subtitle, article body, and a
// subscribe footer. Distinct from LinkedInPostPreview (a feed post) — newsletters lead with a title.
export default function NewsletterArticlePreview({
  title,
  subtitle,
  body,
  author = 'Your Name',
}: NewsletterArticlePreviewProps) {
  return (
    <div className="bg-white rounded-lg border border-gray-200 shadow-sm max-w-lg font-sans overflow-hidden">
      {/* Byline */}
      <div className="flex items-center gap-3 px-5 pt-5">
        <div className="w-10 h-10 rounded-full bg-blue-600 flex items-center justify-center text-white font-bold flex-shrink-0">
          {author.charAt(0).toUpperCase()}
        </div>
        <div className="min-w-0">
          <p className="font-semibold text-gray-900 text-sm truncate">{author}</p>
          <p className="text-xs text-gray-500">Newsletter · Just now</p>
        </div>
      </div>

      {/* Title + subtitle */}
      <div className="px-5 pt-4">
        <h1 className="text-xl font-bold text-gray-900 leading-snug">
          {title || <span className="text-gray-400">Untitled edition</span>}
        </h1>
        {subtitle && <p className="text-sm text-gray-500 mt-1">{subtitle}</p>}
      </div>

      {/* Body */}
      <div className="px-5 py-4">
        <p className="text-sm text-gray-800 whitespace-pre-wrap leading-relaxed">
          {body || <span className="text-gray-400">No content yet.</span>}
        </p>
      </div>

      {/* Subscribe footer, evoking LinkedIn's newsletter card */}
      <div className="px-5 py-3 border-t border-gray-100 flex items-center justify-between">
        <span className="text-xs text-gray-500">Subscribers get this by notification + email</span>
        <button type="button" className="text-blue-600 border border-blue-600 rounded-full px-3 py-1 text-xs font-semibold hover:bg-blue-50">
          Subscribe
        </button>
      </div>
    </div>
  )
}
