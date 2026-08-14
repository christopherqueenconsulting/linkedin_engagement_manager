import { Link } from 'react-router-dom'
import { useAppInfo } from '../../hooks/useAppInfo'
import LegalDisclaimer from './LegalDisclaimer'
import { NAV_ANCHORS } from './navAnchors'

// The marketing page's single footer (issue #1300). The one it replaces hardcoded "© 2024" and sat
// underneath the app's own footer, so a visitor scrolled past two of them with different years.
export default function MarketingFooter() {
  const { data } = useAppInfo()
  const year = new Date().getFullYear()

  return (
    <footer className="bg-ink-900 text-white">
      <div className="max-w-6xl mx-auto px-4 py-12">
        <div className="flex flex-col gap-8 md:flex-row md:items-start md:justify-between">
          <div className="max-w-sm">
            <img
              src="/brand/lem-wordmark-on-dark.png"
              alt="LinkedIn Engagement Manager"
              width={1340}
              height={300}
              className="h-8 w-auto"
              loading="lazy"
            />
            <p className="mt-4 text-sm leading-relaxed text-brand-300">
              Content and engagement for LinkedIn, run inside the caps you set — with you approving
              what ships.
            </p>
          </div>
          {/* Deliberately NOT a second <nav> landmark: the page is meant to expose exactly one, and
              a footer link list that repeats the nav's anchors adds a landmark without adding a
              destination. */}
          <div className="text-sm">
            <h2 className="sr-only">Site links</h2>
            <ul className="flex flex-col gap-3">
              {NAV_ANCHORS.map(({ href, label }) => (
                <li key={href}>
                  <a href={href} className="text-white hover:text-brand-300 transition-colors">
                    {label}
                  </a>
                </li>
              ))}
              <li>
                <Link to="/privacy-policy" className="text-white hover:text-brand-300 transition-colors">
                  Privacy Policy
                </Link>
              </li>
              <li>
                <Link
                  to="/terms-and-conditions"
                  className="text-white hover:text-brand-300 transition-colors"
                >
                  Terms and Conditions
                </Link>
              </li>
            </ul>
          </div>
        </div>
        <div className="mt-10 border-t border-ink-700 pt-6 space-y-2 text-brand-300">
          <LegalDisclaimer />
          <p className="text-xs">
            © {year} Christopher Queen Consulting LLC. All rights reserved.
            {data?.show_version && data.version ? ` · v${data.version}` : ''}
          </p>
        </div>
      </div>
    </footer>
  )
}
