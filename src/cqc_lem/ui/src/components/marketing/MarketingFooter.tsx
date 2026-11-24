import { Link } from 'react-router-dom'
import { useAppInfo } from '../../hooks/useAppInfo'
import LegalDisclaimer from './LegalDisclaimer'
import NavAnchor from './NavAnchor'
import { NAV_ANCHORS } from './navAnchors'

// The marketing page's single footer (issue #1300). The one it replaces hardcoded "© 2024" and sat
// underneath the app's own footer, so a visitor scrolled past two of them with different years.
export default function MarketingFooter() {
  const { data } = useAppInfo()
  const year = new Date().getFullYear()

  return (
    <footer data-theme="dark" className="lem-marketing bg-raised text-fg">
      <div className="box-content max-w-6xl mx-auto px-4 py-12">
        <div className="flex flex-col gap-8 md:flex-row md:items-start md:justify-between">
          <div className="max-w-sm">
            <img
              src="/brand/lem-wordmark-on-dark-v2.png"
              alt="LinkedIn Engagement Manager"
              width={1340}
              height={300}
              className="-ml-[5px] h-8 w-auto"
              decoding="async"
            />
            <p className="mt-4 text-sm leading-relaxed text-fg-muted">
              LinkedIn posts in your voice, published only when you approve them, plus engagement
              suggestions you send yourself.
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
                  <NavAnchor href={href} className="text-fg hover:text-accent transition-colors">
                    {label}
                  </NavAnchor>
                </li>
              ))}
              <li>
                <Link to="/privacy-policy" className="text-fg hover:text-accent transition-colors">
                  Privacy Policy
                </Link>
              </li>
              <li>
                <Link
                  to="/terms-and-conditions"
                  className="text-fg hover:text-accent transition-colors"
                >
                  Terms and Conditions
                </Link>
              </li>
            </ul>
          </div>
        </div>
        <div className="mt-10 border-t border-hairline pt-6 space-y-2 text-fg-muted">
          <LegalDisclaimer className="max-w-measure" />
          <p className="max-w-measure text-xs">
            © {year}{' '}
            <a
              href="https://christopherqueenconsulting.com/"
              className="underline underline-offset-2 hover:text-fg transition-colors"
            >
              Christopher Queen Consulting
            </a>{' '}
            LLC. All rights reserved.
            {data?.show_version && data.version ? ` · v${data.version}` : ''}
          </p>
        </div>
      </div>
    </footer>
  )
}
