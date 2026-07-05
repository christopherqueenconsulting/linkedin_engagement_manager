import type { ReactNode } from 'react'

// Reusable wrapper for the repeated settings-card markup used across the Account page.
export default function SettingsCard({
  title,
  subtitle,
  children,
  headerRight,
  className = '',
}: {
  title?: string
  subtitle?: string
  children: ReactNode
  headerRight?: ReactNode
  className?: string
}) {
  const hasHeader = !!title || !!subtitle || !!headerRight
  return (
    <div className={`bg-white rounded-lg shadow-sm border border-gray-200 p-6 space-y-4 ${className}`}>
      {hasHeader && (
        <div className="flex items-center justify-between">
          <div>
            {title && <h2 className="text-base font-semibold text-gray-700">{title}</h2>}
            {subtitle && <p className="text-xs text-gray-500 mt-1">{subtitle}</p>}
          </div>
          {headerRight}
        </div>
      )}
      {children}
    </div>
  )
}
