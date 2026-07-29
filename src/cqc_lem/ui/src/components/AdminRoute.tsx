import { Navigate } from 'react-router-dom'
import { useAuth } from '../contexts/AuthContext'

export default function AdminRoute({ children }: { children: React.ReactNode }) {
  const { user, isLoading, isAdmin, openLoginModal } = useAuth()

  if (isLoading) {
    return (
      <div className="flex items-center justify-center h-40 text-gray-400">
        Loading…
      </div>
    )
  }

  if (!user) {
    openLoginModal()
    return <Navigate to="/" replace />
  }

  if (!isAdmin) {
    return <Navigate to="/" replace />
  }

  return <>{children}</>
}
