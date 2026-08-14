import { useEffect, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { useQueryClient } from '@tanstack/react-query'
import { useAccountReadiness } from '../hooks/useAccountReadiness'
import SuppressionBanner from '../components/SuppressionBanner'
import AffiliateNotice from '../components/AffiliateNotice'
import SettingsHub from './account/settings/SettingsHub'

export default function Account() {
  const queryClient = useQueryClient()
  const { data: readiness } = useAccountReadiness()
  const [searchParams, setSearchParams] = useSearchParams()
  // The LinkedIn OAuth callback lands as ?li_connected=1 or ?li_error=… and the effect below
  // strips it from the URL, so what it said is read ONCE at mount — otherwise the notice would
  // vanish with the parameter that produced it.
  const [oauthResult] = useState(() => {
    if (searchParams.get('li_connected') === '1') {
      return { ok: true, text: 'LinkedIn connected successfully!', dismissAfterMs: 5000 }
    }
    if (searchParams.get('li_error')) {
      return { ok: false, text: 'LinkedIn connection failed — please try again.', dismissAfterMs: 8000 }
    }
    return null
  })
  const [oauthDismissed, setOauthDismissed] = useState(false)
  const oauthMsg = oauthResult && !oauthDismissed ? oauthResult : null

  // Everything the callback has to change lives outside React: the connected marker, the cached
  // token status, and the URL itself.
  useEffect(() => {
    if (!oauthResult) return
    if (oauthResult.ok) {
      localStorage.setItem('lem_li_connected', '1')
      // Force refetch so the fresh token is reflected immediately
      queryClient.invalidateQueries({ queryKey: ['token-status'] })
    }
    setSearchParams({ section: 'setup' }, { replace: true })
    const t = setTimeout(() => setOauthDismissed(true), oauthResult.dismissAfterMs)
    return () => clearTimeout(t)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  return (
    <div className="max-w-5xl space-y-6">
      <h1 className="text-2xl font-bold text-gray-800">Settings</h1>

      {oauthMsg && (
        <p className={`text-sm font-medium ${oauthMsg.ok ? 'text-green-600' : 'text-red-600'}`}>
          {oauthMsg.text}
        </p>
      )}

      {/* Silent-suppression tripwire (issue #629) — the only notice a limited account ever gets */}
      <SuppressionBanner />

      {/* Affiliate enrollment notice (issue #737) — default-on enrollment is only fair if it is
          announced, so this shows until the user acknowledges it. */}
      <AffiliateNotice />

      {/* Account setup checklist — exactly what automation needs, from the readiness API */}
      {readiness && !readiness.ready && (
        <div className="bg-amber-50 rounded-lg border border-amber-200 p-4">
          <p className="text-sm font-semibold text-amber-900 mb-3">Finish account setup</p>
          <div className="space-y-2">
            {readiness.items.map((it) => (
              <div key={it.key} className="flex items-start gap-3">
                <span className={`mt-0.5 w-5 h-5 rounded-full flex items-center justify-center text-[11px] font-bold flex-shrink-0 ${it.ok ? 'bg-green-500 text-white' : 'bg-gray-200 text-gray-500'}`}>
                  {it.ok ? '✓' : '!'}
                </span>
                <div>
                  <span className={`text-sm ${it.ok ? 'text-gray-400 line-through' : 'text-gray-800 font-medium'}`}>
                    {it.label}
                    {it.required && !it.ok && <span className="text-red-500"> *</span>}
                  </span>
                  {!it.ok && it.hint && <p className="text-xs text-gray-500">{it.hint}</p>}
                </div>
              </div>
            ))}
          </div>
        </div>
      )}

      <SettingsHub />
    </div>
  )
}
