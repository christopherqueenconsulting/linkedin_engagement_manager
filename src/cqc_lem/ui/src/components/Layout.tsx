import { NavLink, Outlet, useNavigate, useLocation } from 'react-router-dom'
import { useAuth } from '../contexts/AuthContext'
import AccountReadinessBanner from './AccountReadinessBanner'
import FeedbackWidget from './FeedbackWidget'
import Footer from './Footer'
import PostHogSurveyModal from './PostHogSurveyModal'
import ShippedNotice from './ShippedNotice'
import SurveyModal from './SurveyModal'

// Pages whose features depend on a fully set-up account — show the readiness banner here.
const AUTOMATION_PATHS = ['/content', '/schedule', '/review', '/avatars']

const navLinks = [
  { to: '/', label: 'Home', end: true },
  { to: '/account', label: 'Account' },
  { to: '/avatars', label: 'Avatars' },
  { to: '/content', label: 'Content' },
]

export default function Layout() {
  const { user, logout, openLoginModal } = useAuth()
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
        <div className="max-w-5xl mx-auto px-4 flex items-center gap-6 h-14">
          <span className="font-bold text-blue-600 text-lg">LEM</span>
          {user && navLinks.map(({ to, label, end }) => (
            <NavLink
              key={to}
              to={to}
              end={end}
              className={({ isActive }) =>
                `text-sm font-medium transition-colors ${
                  isActive
                    ? 'text-blue-600 border-b-2 border-blue-600 pb-0.5'
                    : 'text-gray-600 hover:text-gray-900'
                }`
              }
            >
              {label}
            </NavLink>
          ))}
          <div className="ml-auto flex items-center gap-3">
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
        {showReadiness && <AccountReadinessBanner />}
        {/* "You asked, we shipped" + its micro-CSAT — renders only when a fix this user
            reported has shipped and they haven't acknowledged it yet (issue #502) */}
        {user && <ShippedNotice />}
        <Outlet />
      </main>
      <Footer />
      <FeedbackWidget />
      {/* Survey modal — renders only when the server says a survey is due (issue #501) */}
      {user && <SurveyModal />}
      {/* PostHog-targeted NPS/CSAT, drawn in LEM's own chrome (issue #653). Stands down whenever
          the bespoke modal above has something to ask, so the two can never stack. */}
      {user && <PostHogSurveyModal />}
    </div>
  )
}
