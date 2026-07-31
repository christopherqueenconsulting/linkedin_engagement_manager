import { useEffect } from 'react'
import { BrowserRouter, Routes, Route, Navigate, useSearchParams, useLocation } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { AuthProvider, useAuth } from './contexts/AuthContext'
import Layout from './components/Layout'
import ProtectedRoute from './components/ProtectedRoute'
import AdminRoute from './components/AdminRoute'
import LoginModal from './components/LoginModal'
import NewVersionNotice from './components/NewVersionNotice'
import Dashboard from './pages/Dashboard'
import Account from './pages/Account'
import Avatars from './pages/Avatars'
import ContentStudio from './pages/ContentStudio'
import AdminFeedbackPage from './pages/AdminFeedbackPage'
import Landing from './pages/Landing'
import PrivacyPolicy from './pages/PrivacyPolicy'
import TermsAndConditions from './pages/TermsAndConditions'
import { capturePageview } from './utils/analytics'

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

function AppRoutes() {
  const { user, isLoginModalOpen } = useAuth()
  usePageviews()

  return (
    <>
      {isLoginModalOpen && <LoginModal />}
      <NewVersionNotice />
      <Routes>
        <Route path="/" element={<Layout />}>
          <Route index element={user ? <Dashboard /> : <Landing />} />
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
          {/* Public legal pages — reachable from the footer and from logged-out landing pages */}
          <Route path="privacy-policy" element={<PrivacyPolicy />} />
          <Route path="terms-and-conditions" element={<TermsAndConditions />} />
          {/* Legacy routes → consolidated Content Studio */}
          <Route path="schedule" element={<Navigate to="/content" replace />} />
          <Route path="review" element={<ProtectedRoute><LegacyReviewRedirect /></ProtectedRoute>} />
        </Route>
      </Routes>
    </>
  )
}

export default function App() {
  return (
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <AuthProvider>
          <AppRoutes />
        </AuthProvider>
      </BrowserRouter>
    </QueryClientProvider>
  )
}
