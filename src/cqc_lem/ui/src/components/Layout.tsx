import { Suspense } from 'react'
import { NavLink, Outlet, useNavigate, useLocation } from 'react-router-dom'
import { useAuth } from '../contexts/useAuth'
import AccountReadinessBanner from './AccountReadinessBanner'
import FeedbackWidget from './FeedbackWidget'
import FloatingDock from './FloatingDock'
import Footer from './Footer'
import PostHogSurveyModal from './PostHogSurveyModal'
import ShippedNotice from './ShippedNotice'
import StrongFactorGate from './StrongFactorGate'
import StrongFactorPrompt from './StrongFactorPrompt'
import SurveyModal from './SurveyModal'

// Pages whose features depend on a fully set-up account — show the readiness banner here.
const AUTOMATION_PATHS = ['/content', '/schedule', '/review', '/avatars']

const navLinks = [
  { to: '/', label: 'Home', end: true },
  { to: '/account', label: 'Account' },
  { to: '/avatars', label: 'Avatars' },
  { to: '/content', label: 'Content' },
]

const adminNavLinks = [
  { to: '/admin/feedback', label: 'Feedback Triage' },
]

export default function Layout() {
  const { user, logout, openLoginModal, isAdmin, enrollmentRequired } = useAuth()
  const navigate = useNavigate()
  const location = useLocation()
  const showReadiness = !!user && AUTOMATION_PATHS.some((p) => location.pathname.startsWith(p))

  async function handleLogout() {
    await logout()
    navigate('/')
  }

  return (
    <div className="min-h-screen flex flex-col bg-gray-100">
      <nav className="bg-white border-b border-gray-200 sticky top-0 z-10">
        <div className="max-w-5xl mx-auto px-4 flex items-center gap-3 sm:gap-6 h-14">
          <span className="font-bold text-blue-600 text-lg shrink-0">LEM</span>
          {/* On a phone the links no longer push the account controls off the right edge: this row
              is the only thing allowed to shrink, and it scrolls sideways instead (issue #894). */}
          {user && (
            <div
              data-testid="nav-links"
              className="flex items-center gap-4 sm:gap-6 min-w-0 overflow-x-auto scrollbar-none"
            >
              {navLinks.map(({ to, label, end }) => (
                <NavLink
                  key={to}
                  to={to}
                  end={end}
                  className={({ isActive }) =>
                    `text-sm font-medium transition-colors whitespace-nowrap ${
                      isActive
                        ? 'text-blue-600 border-b-2 border-blue-600 pb-0.5'
                        : 'text-gray-600 hover:text-gray-900'
                    }`
                  }
                >
                  {label}
                </NavLink>
              ))}
              {isAdmin && adminNavLinks.map(({ to, label }) => (
                <NavLink
                  key={to}
                  to={to}
                  className={({ isActive }) =>
                    `text-sm font-medium transition-colors whitespace-nowrap ${
                      isActive
                        ? 'text-purple-600 border-b-2 border-purple-600 pb-0.5'
                        : 'text-gray-600 hover:text-gray-900'
                    }`
                  }
                >
                  {label}
                </NavLink>
              ))}
            </div>
          )}
          <div className="ml-auto flex items-center gap-3 shrink-0">
            {user ? (
              <>
                <span className="text-xs text-gray-500 hidden sm:inline">
                  Logged in as: <span className="font-medium text-gray-700">{user.email}</span>
                </span>
                <button
                  onClick={handleLogout}
                  className="text-xs text-red-500 hover:text-red-700 font-medium border border-red-200 hover:border-red-400 px-2.5 py-1 rounded transition-colors"
                >
                  Log out
                </button>
              </>
            ) : (
              <button
                onClick={openLoginModal}
                className="text-xs text-blue-600 hover:text-blue-800 font-medium border border-blue-200 hover:border-blue-400 px-2.5 py-1 rounded transition-colors"
              >
                Login / Sign Up
              </button>
            )}
          </div>
        </div>
      </nav>
      <main className="flex-1 w-full max-w-5xl mx-auto px-4 py-6">
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
