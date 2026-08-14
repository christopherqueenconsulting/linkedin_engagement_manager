import { Suspense, useEffect } from 'react'
import { BrowserRouter, Routes, Route, Navigate, useSearchParams, useLocation } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { AuthProvider, useAuth } from './contexts/AuthContext'
import Layout from './components/Layout'
import MarketingLayout from './components/marketing/MarketingLayout'
import ProtectedRoute from './components/ProtectedRoute'
import AdminRoute from './components/AdminRoute'
import LoginModal from './components/LoginModal'
import NewVersionNotice from './components/NewVersionNotice'
import Landing from './pages/Landing'
import PrivacyPolicy from './pages/PrivacyPolicy'
import TermsAndConditions from './pages/TermsAndConditions'
import { capturePageview } from './utils/analytics'
import { lazyWithChunkRecovery } from './utils/chunkReload'

// The authenticated app is code-split (issue #1300). An anonymous visitor reading the marketing
// page used to download Dashboard, ContentStudio, Account, Avatars and AdminFeedbackPage — every
// screen they cannot reach — inside a single ~739 KB entry chunk.
//
// `lazyWithChunkRecovery` rather than bare `React.lazy`: releases batch 4x daily, and a tab open
// across one loses the chunk hashes it had not fetched yet. That helper (issue #743) turns the
// resulting "Failed to fetch dynamically imported module" into one guarded reload. Splitting the
// routes is also what finally gives that machinery something to protect — until now the SPA was
// effectively one chunk, so the stale-chunk path almost never ran.
const Dashboard = lazyWithChunkRecovery(() => import('./pages/Dashboard'))
const Account = lazyWithChunkRecovery(() => import('./pages/Account'))
const Avatars = lazyWithChunkRecovery(() => import('./pages/Avatars'))
const ContentStudio = lazyWithChunkRecovery(() => import('./pages/ContentStudio'))
const AdminFeedbackPage = lazyWithChunkRecovery(() => import('./pages/AdminFeedbackPage'))

const queryClient = new QueryClient()

// Backward-compat: the old /review page defaulted to the posts-review list, and /review?tab=X
// deep-linked newsletters/dms. Map those to the consolidated /content tabs. Bare /review must go
// to the review tab (NOT the compose default).
function LegacyReviewRedirect() {
  const [params] = useSearchParams()
  const tab = params.get('tab')
  const target = tab === 'newsletters' || tab === 'dms' ? tab : 'review'
  return <Navigate to={`/content?tab=${target}`} replace />
}

// posthog's own pageview listener only fires on a full page load, so a SPA route change would be
// invisible. The search string is part of the key — /content?tab=dms is a different screen.
function usePageviews() {
  const { pathname, search } = useLocation()
  useEffect(() => { capturePageview() }, [pathname, search])
}

// What a lazy route renders while its chunk is in flight. Deliberately not a spinner: the chunk
// usually arrives in a frame or two, and a spinner that flashes is worse than a quiet gap.
function RouteFallback() {
  return <div className="min-h-[50vh]" aria-busy="true" aria-live="polite" />
}

/**
 * The neutral shell a visitor sees for the one `/auth/session` round-trip.
 *
 * `isLoading` starts true with `user` still null, so rendering the logged-out branch immediately
 * would show a returning, signed-in user the entire marketing page — hero, pricing and all — and
 * then swap it for the app. That was survivable while both branches rendered inside the same
 * chrome; with the marketing page hoisted out of `Layout` it is a full-page flash on every hard
 * refresh.
 */
function SessionBoot() {
  return <div className="min-h-screen bg-white" aria-busy="true" />
}

function AppRoutes() {
  const { user, isLoading, isLoginModalOpen } = useAuth()
  usePageviews()

  return (
    <>
      {isLoginModalOpen && <LoginModal />}
      <NewVersionNotice />
      {isLoading ? (
        <SessionBoot />
      ) : (
        <Routes>
          {user ? (
            <Route path="/" element={<Layout />}>
              <Route index element={<Dashboard />} />
              <Route
                path="account"
                element={<ProtectedRoute><Account /></ProtectedRoute>}
              />
              <Route
                path="avatars"
                element={<ProtectedRoute><Avatars /></ProtectedRoute>}
              />
              <Route
                path="content"
                element={<ProtectedRoute><ContentStudio /></ProtectedRoute>}
              />
              <Route
                path="admin/feedback"
                element={<AdminRoute><AdminFeedbackPage /></AdminRoute>}
              />
              <Route path="privacy-policy" element={<PrivacyPolicy />} />
              <Route path="terms-and-conditions" element={<TermsAndConditions />} />
              {/* Legacy routes → consolidated Content Studio */}
              <Route path="schedule" element={<Navigate to="/content" replace />} />
              <Route path="review" element={<ProtectedRoute><LegacyReviewRedirect /></ProtectedRoute>} />
            </Route>
          ) : (
            /* Logged out: the marketing chrome, and the legal pages ride inside it. Sending a
               prospect from the marketing footer into the application's nav and narrow measure
               reads as having left the site mid-funnel. */
            <Route path="/" element={<MarketingLayout />}>
              <Route index element={<Landing />} />
              <Route path="privacy-policy" element={<PrivacyPolicy />} />
              <Route path="terms-and-conditions" element={<TermsAndConditions />} />
              {/* Anything else a logged-out visitor lands on (a bookmarked /content, a stale link)
                  belongs on the front page rather than in an empty app shell — via ProtectedRoute,
                  which is what also opens the login modal on the way. */}
              <Route path="*" element={<ProtectedRoute><Navigate to="/" replace /></ProtectedRoute>} />
            </Route>
          )}
        </Routes>
      )}
    </>
  )
}

export default function App() {
  return (
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <AuthProvider>
          {/* Every lazy route needs a boundary above it or React throws instead of suspending.
              One here covers both trees; Layout adds a second inside its chrome so an in-app
              navigation does not blank the nav and footer with it. */}
          <Suspense fallback={<RouteFallback />}>
            <AppRoutes />
          </Suspense>
        </AuthProvider>
      </BrowserRouter>
    </QueryClientProvider>
  )
}
