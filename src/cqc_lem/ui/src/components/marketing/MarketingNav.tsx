import { useState } from 'react'
import { Link } from 'react-router-dom'
import { useAuth } from '../../contexts/AuthContext'
import CtaButton from './CtaButton'
import Icon from './Icon'
import { NAV_ANCHORS } from './navAnchors'

// The marketing page's own nav (issue #1300) — and the ONLY one it renders. The page used to draw
// this bar while ALSO rendering inside the app's `Layout`, so a logged-out visitor got two stacked
// `sticky top-0` bars and two footers.

export default function MarketingNav() {
  const { openLoginModal } = useAuth()
  const [menuOpen, setMenuOpen] = useState(false)

  return (
    <nav
      aria-label="Main"
      className="sticky top-0 z-20 border-b border-line-200 bg-white/95 backdrop-blur"
    >
      <div className="max-w-6xl mx-auto px-4 h-16 flex items-center gap-4">
        <Link to="/" className="flex items-center shrink-0" aria-label="LinkedIn Engagement Manager — home">
          <img
            src="/brand/lem-wordmark.png"
            alt="LinkedIn Engagement Manager"
            width={1340}
            height={300}
            className="h-7 w-auto sm:h-8"
            fetchPriority="high"
          />
        </Link>

        <div className="hidden md:flex items-center gap-6 ml-4">
          {NAV_ANCHORS.map(({ href, label }) => (
            <a
              key={href}
              href={href}
              className="text-sm font-medium text-ink-600 hover:text-brand-700 transition-colors focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand-600"
            >
              {label}
            </a>
          ))}
        </div>

        <div className="ml-auto flex items-center gap-2 shrink-0">
          <button
            type="button"
            onClick={openLoginModal}
            className="min-h-11 px-2 sm:px-4 text-sm font-semibold text-ink-700 hover:text-brand-700 transition-colors focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand-600"
          >
            Log in
          </button>
          <CtaButton
            section="nav"
            cta="nav_trial"
            label="Start free trial from the top navigation"
            onClick={openLoginModal}
            className="hidden sm:inline-flex px-4 py-2 text-sm"
          >
            Start free trial
          </CtaButton>
          <button
            type="button"
            aria-expanded={menuOpen}
            aria-controls="marketing-nav-sheet"
            aria-label={menuOpen ? 'Close the navigation menu' : 'Open the navigation menu'}
            onClick={() => setMenuOpen((open) => !open)}
            className="md:hidden inline-flex items-center justify-center h-11 w-11 rounded-lg text-ink-700 hover:bg-surface-50 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand-600"
          >
            <Icon name={menuOpen ? 'close' : 'menu'} className="h-6 w-6" />
          </button>
        </div>
      </div>

      {menuOpen && (
        <div id="marketing-nav-sheet" className="md:hidden border-t border-line-200 bg-white px-4 py-3">
          <ul className="flex flex-col">
            {NAV_ANCHORS.map(({ href, label }) => (
              <li key={href}>
                <a
                  href={href}
                  onClick={() => setMenuOpen(false)}
                  className="flex items-center min-h-11 text-base font-medium text-ink-700 hover:text-brand-700"
                >
                  {label}
                </a>
              </li>
            ))}
            <li className="pt-2">
              <CtaButton
                section="nav"
                cta="nav_sheet_trial"
                label="Start free trial from the navigation menu"
                onClick={() => {
                  setMenuOpen(false)
                  openLoginModal()
                }}
                className="w-full"
              >
                Start free trial
              </CtaButton>
            </li>
          </ul>
        </div>
      )}
    </nav>
  )
}
