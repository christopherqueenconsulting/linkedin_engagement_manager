import { useEffect } from 'react'
import { Navigate } from 'react-router-dom'
import { useAuth } from '../contexts/useAuth'

export default function ProtectedRoute({ children }: { children: React.ReactNode }) {
  const { user, isLoading, openLoginModal } = useAuth()

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

  return <>{children}</>
}
