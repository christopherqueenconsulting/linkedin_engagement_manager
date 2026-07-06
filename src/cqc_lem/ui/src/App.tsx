import { BrowserRouter, Routes, Route, Navigate, useSearchParams } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { AuthProvider, useAuth } from './contexts/AuthContext'
import Layout from './components/Layout'
import ProtectedRoute from './components/ProtectedRoute'
import LoginModal from './components/LoginModal'
import Dashboard from './pages/Dashboard'
import Account from './pages/Account'
import Avatars from './pages/Avatars'
import ContentStudio from './pages/ContentStudio'
import Landing from './pages/Landing'

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

function AppRoutes() {
  const { user, isLoginModalOpen } = useAuth()

  return (
    <>
      {isLoginModalOpen && <LoginModal />}
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
