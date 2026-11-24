import { Suspense, useEffect, useState } from 'react'
import { NavLink, Outlet, useNavigate, useLocation } from 'react-router-dom'
import { useAuth } from '../contexts/useAuth'
import { FLAGS, useFeatureFlag } from '../hooks/useFeatureFlags'
import AccountReadinessBanner from './AccountReadinessBanner'
import FeedbackWidget from './FeedbackWidget'
import FloatingDock, { OPEN_FEEDBACK_EVENT } from './FloatingDock'
import Icon from './marketing/Icon'
import Footer from './Footer'
import PostHogSurveyModal from './PostHogSurveyModal'
import ShippedNotice from './ShippedNotice'
import StrongFactorGate from './StrongFactorGate'
import StrongFactorPrompt from './StrongFactorPrompt'
import SurveyModal from './SurveyModal'

// Pages whose features depend on a fully set-up account — show the readiness banner here.
const AUTOMATION_PATHS = ['/content', '/schedule', '/review', '/avatars']

const navLinks: { to: string; label: string; end?: boolean }[] = [
  { to: '/', label: 'Home', end: true },
  { to: '/account', label: 'Account' },
  { to: '/avatars', label: 'Avatars' },
  { to: '/content', label: 'Content' },
]

// Shown only while `mcp-server-enabled` is on for this user (issue #2687). The route redirects home
// when it is off, so a hidden entry and an unreachable page always agree.
const agentsNavLink: { to: string; label: string; end?: boolean } = { to: '/agents', label: 'Agents' }

// Shown only while `weekly-report-enabled` is on (issue #2667); the page redirects home when off.
const reportNavLink: { to: string; label: string; end?: boolean } = { to: '/report', label: 'Report' }

// One link style for every nav item: a full-height row with a 2px primary bar under the active
// page (the marketing nav's hairline chrome, carried into the app).
function navLinkClass({ isActive }: { isActive: boolean }): string {
  return `inline-flex h-16 items-center border-b-2 text-sm whitespace-nowrap transition-colors ${
    isActive
      ? 'border-accent font-semibold text-fg'
      : 'border-transparent font-medium text-fg-muted hover:text-fg'
  }`
}

const adminNavLinks = [
  { to: '/admin/users', label: 'Users' },
  { to: '/admin/feedback', label: 'Feedback Triage' },
  { to: '/admin/audit-log', label: 'Audit Log' },
]

export default function Layout() {
  const { user, logout, openLoginModal, isAdmin, enrollmentRequired } = useAuth()
  const agentsEnabled = useFeatureFlag(FLAGS.mcpServer, false)
  const reportEnabled = useFeatureFlag(FLAGS.weeklyReport, false)
  const links = [...navLinks, ...(reportEnabled ? [reportNavLink] : []), ...(agentsEnabled ? [agentsNavLink] : [])]
  const navigate = useNavigate()
  const location = useLocation()
  const showReadiness = !!user && AUTOMATION_PATHS.some((p) => location.pathname.startsWith(p))
  // Below md the links live behind one menu button (the wordmark leaves no room for a row of
  // them on a phone); it closes on Escape and on every route change.
  // Keyed on the path it was opened on, so navigating away closes it without an effect.
  const [menuPath, setMenuPath] = useState<string | null>(null)
  const menuOpen = menuPath === location.pathname
  useEffect(() => {
    if (!menuOpen) return
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') setMenuPath(null) }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [menuOpen])

  async function handleLogout() {
    await logout()
    navigate('/')
  }

  return (
    <div className="lem-app min-h-screen flex flex-col bg-page">
      <a
        href="#main"
        className="sr-only focus:not-sr-only focus:absolute focus:left-4 focus:top-2 focus:z-20 focus:rounded-control focus:bg-raised focus:px-3 focus:py-2 focus:text-sm focus:font-semibold focus:text-fg focus:ring-2 focus:ring-accent"
      >
        Skip to main content
      </a>
      <nav aria-label="Main" className="bg-raised border-b border-hairline sticky top-0 z-10">
        <div className="box-content max-w-6xl mx-auto px-4 flex items-center gap-4 sm:gap-8 h-16">
          {/* -ml-[5px]: the artwork has 50 of 1340 px transparent on its left, about 5px here, so
              the visible mark starts on the page's left edge (same as the marketing nav). */}
          <NavLink to="/" className="shrink-0" aria-label="LinkedIn Engagement Manager, home">
            <img
              src="/brand/lem-wordmark.png"
              alt=""
              width={1340}
              height={300}
              className="-ml-[5px] h-7 w-auto"
            />
          </NavLink>
          {/* From md the links sit inline; this row is the only thing allowed to shrink, and it
              scrolls sideways rather than push the account controls off the edge (issue #894). */}
          {user && (
            <div
              data-testid="nav-links"
              className="hidden md:flex items-center gap-7 min-w-0 overflow-x-auto scrollbar-none"
            >
              {links.map(({ to, label, end }) => (
                <NavLink key={to} to={to} end={end} className={navLinkClass}>
                  {label}
                </NavLink>
              ))}
              {isAdmin && adminNavLinks.map(({ to, label }) => (
                <NavLink key={to} to={to} className={navLinkClass}>
                  {label}
                </NavLink>
              ))}
            </div>
          )}
          <div className="ml-auto flex items-center gap-3 shrink-0">
            {user ? (
              <>
                <span className="text-sm text-fg-muted hidden md:inline">
                  Logged in as: <span className="font-medium text-fg">{user.email}</span>
                </span>
                <button
                  onClick={handleLogout}
                  className="hidden md:inline-flex items-center min-h-11 px-2 text-sm font-medium text-fg-muted hover:text-fg transition-colors"
                >
                  Log out
                </button>
                <button
                  type="button"
                  onClick={() => setMenuPath(menuOpen ? null : location.pathname)}
                  aria-expanded={menuOpen}
                  aria-controls="app-menu"
                  aria-label={menuOpen ? 'Close menu' : 'Menu'}
                  className="md:hidden inline-flex h-11 w-11 items-center justify-center rounded-control border border-field text-fg hover:bg-page"
                >
                  <Icon name={menuOpen ? 'close' : 'menu'} className="h-5 w-5" />
                </button>
              </>
            ) : (
              <button
                onClick={openLoginModal}
                className="min-h-11 px-2 text-sm font-semibold text-accent hover:underline"
              >
                Login / Sign Up
              </button>
            )}
          </div>
        </div>
        {user && menuOpen && (
          <div id="app-menu" className="md:hidden border-t border-hairline bg-raised px-4 pb-3">
            <p className="py-3 text-sm text-fg-muted truncate">
              Logged in as: <span className="font-medium text-fg">{user.email}</span>
            </p>
            <ul className="divide-y divide-hairline border-y border-hairline">
              {[...links, ...(isAdmin ? adminNavLinks : [])].map(({ to, label, end }: { to: string; label: string; end?: boolean }) => (
                <li key={to}>
                  <NavLink
                    to={to}
                    end={end}
                    className={({ isActive }) =>
                      `flex min-h-11 items-center text-sm ${isActive ? 'font-semibold text-fg' : 'font-medium text-fg-muted hover:text-fg'}`
                    }
                  >
                    {label}
                  </NavLink>
                </li>
              ))}
            </ul>
            <button
              onClick={() => {
                setMenuPath(null)
                window.dispatchEvent(new Event(OPEN_FEEDBACK_EVENT))
              }}
              className="flex min-h-11 w-full items-center border-b border-hairline text-sm font-medium text-fg-muted hover:text-fg"
            >
              Feedback / Report a bug
            </button>
            <button
              onClick={handleLogout}
              className="mt-2 flex min-h-11 w-full items-center text-sm font-medium text-fg-muted hover:text-fg"
            >
              Log out
            </button>
          </div>
        )}
      </nav>
      <main id="main" tabIndex={-1} className="flex-1 w-full max-w-[74rem] mx-auto px-4 py-8 sm:py-10 focus:outline-none">
        {/* Mandatory strong-factor enrolment (issue #905). The gate REPLACES the page: a held
            session is refused everywhere but the enrolment surface, so rendering the app behind it
            would only produce 403s. The prompt is the pre-deadline half. */}
        {enrollmentRequired ? (
          <StrongFactorGate />
        ) : (
          <>
            {showReadiness && <AccountReadinessBanner />}
            {user && <StrongFactorPrompt />}
            {/* "You asked, we shipped" + its micro-CSAT — renders only when a fix this user
                reported has shipped and they haven't acknowledged it yet (issue #502) */}
            {user && <ShippedNotice />}
            {/* The app's routes are lazy since issue #1300, and a lazy route with no boundary
                above it throws instead of suspending. This one sits INSIDE the chrome on purpose:
                a boundary further up would blank the nav and the footer for the frame a chunk
                takes to arrive. */}
            <Suspense fallback={<div className="h-40" aria-busy="true" />}>
              <Outlet />
            </Suspense>
          </>
        )}
      </main>
      <Footer />
      {/* Shared bottom-right stack — page-level floating controls (Settings' Save All) portal in
          here so nothing can be buried under the feedback launcher again (issue #596). */}
      <FloatingDock>
        <FeedbackWidget />
      </FloatingDock>
      {/* Survey modal — renders only when the server says a survey is due (issue #501). Both
          modals stand down while a session is held to enrolment: they poll endpoints outside that
          surface, and asking someone to rate us mid-lockout is the wrong moment anyway. */}
      {user && !enrollmentRequired && <SurveyModal />}
      {/* PostHog-targeted NPS/CSAT, drawn in LEM's own chrome (issue #653). Stands down whenever
          the bespoke modal above has something to ask, so the two can never stack. */}
      {user && !enrollmentRequired && <PostHogSurveyModal />}
    </div>
  )
}
