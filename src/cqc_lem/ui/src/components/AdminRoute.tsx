import { useEffect } from 'react'
import { Navigate } from 'react-router-dom'
import { useAuth } from '../contexts/AuthContext'

export default function AdminRoute({ children }: { children: React.ReactNode }) {
  const { user, isLoading, isAdmin, openLoginModal } = useAuth()

  // Same shape as ProtectedRoute: opening the modal is a state update on another component, so it
  // has to happen in an effect, not during render.
  useEffect(() => {
    if (!isLoading && !user) {
      openLoginModal()
    }
  }, [isLoading, user, openLoginModal])

  if (isLoading) {
    return (
      <div className="flex items-center justify-center h-40 text-gray-400">
        Loading…
      </div>
    )
  }

  if (!user) {
    return <Navigate to="/" replace />
  }

  if (!isAdmin) {
    return <Navigate to="/" replace />
  }

  return <>{children}</>
}
