import { useState } from 'react'
import { Link } from 'react-router-dom'
import { useAuth } from '../../contexts/useAuth'
import CtaButton from './CtaButton'
import Icon from './Icon'
import NavAnchor from './NavAnchor'
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
      className="sticky top-0 z-20 border-b border-hairline bg-raised"
    >
      <div className="box-content max-w-6xl mx-auto px-4 h-16 flex items-center gap-4">
        <Link to="/" className="flex items-center shrink-0" aria-label="LinkedIn Engagement Manager — home">
          {/* The charcoal wordmark vanishes on the dark nav, so the dark scheme gets the on-dark cut:
              v2 is the official on-dark PNG with only "Manager" recoloured to the pack's dark primary
              (owner ruling 101), since brand blue on charcoal is under 2:1. */}
          {/* -ml-[5px]: the artwork has about 11 of 286 px transparent on its left, about 5px at this
              height, so the visible mark starts on the page's one left edge. */}
          <picture>
            <source srcSet="/brand/lem-wordmark-on-dark-v2.png" media="(prefers-color-scheme: dark)" />
            <img
              src="/brand/lem-wordmark.png"
              alt="LinkedIn Engagement Manager"
              width={286}
              height={64}
              className="-ml-[5px] h-7 w-auto sm:h-8"
              fetchPriority="high"
            />
          </picture>
        </Link>

        {/* Inline links from lg (design pass 2026-11-24): at 768px five links, Log in and the trial
            button did not fit one row and "How it works" wrapped to three lines. Below lg the
            menu button and sheet carry them. */}
        <div className="hidden lg:flex items-center gap-6 ml-4">
          {NAV_ANCHORS.map(({ href, label }) => (
            <NavAnchor
              key={href}
              href={href}
              className="text-sm font-medium text-fg-muted hover:text-accent transition-colors focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent"
            >
              {label}
            </NavAnchor>
          ))}
        </div>

        <div className="ml-auto flex items-center gap-2 shrink-0">
          <button
            type="button"
            onClick={openLoginModal}
            className="min-h-11 px-2 sm:px-4 text-sm font-semibold text-fg hover:text-accent transition-colors focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent"
          >
            Log in
          </button>
          {/* The WRAPPER is what hides below `sm`, not the button (issue #1556). `hidden` handed to
              CtaButton lost to the `inline-flex` in its own base classes — same specificity, and
              Tailwind emits `inline-flex` last — so this 162px button stayed on screen at 375px and
              pushed the nav row to 443px. */}
          <span className="hidden sm:block">
            <CtaButton
              section="nav"
              cta="nav_trial"
              label="Start free trial from the top navigation"
              variant="ghost"
              onClick={openLoginModal}
              className="px-4 py-2 text-sm"
            >
              Start free trial
            </CtaButton>
          </span>
          <button
            type="button"
            aria-expanded={menuOpen}
            aria-controls="marketing-nav-sheet"
            aria-label={menuOpen ? 'Close the navigation menu' : 'Open the navigation menu'}
            onClick={() => setMenuOpen((open) => !open)}
            className="lg:hidden inline-flex items-center justify-center h-11 w-11 rounded-lg text-fg hover:bg-page focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent"
          >
            <Icon name={menuOpen ? 'close' : 'menu'} className="h-6 w-6" />
          </button>
        </div>
      </div>

      {menuOpen && (
        <div id="marketing-nav-sheet" className="lg:hidden border-t border-hairline bg-raised px-4 py-3">
          <ul className="flex flex-col">
            {NAV_ANCHORS.map(({ href, label }) => (
              <li key={href}>
                <NavAnchor
                  href={href}
                  onNavigate={() => setMenuOpen(false)}
                  className="flex items-center min-h-11 text-base font-medium text-fg hover:text-accent"
                >
                  {label}
                </NavAnchor>
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
