import { useEffect, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { useQueryClient } from '@tanstack/react-query'
import { useAccountReadiness } from '../hooks/useAccountReadiness'
import SubscriptionCard from './account/SubscriptionCard'
import VideoCreditsCard from './account/VideoCreditsCard'
import TimezoneCard from './account/TimezoneCard'
import LoginLocationCard from './account/LoginLocationCard'
import InactivityCard from './account/InactivityCard'
import LinkedInLoginCard from './account/LinkedInLoginCard'
import CompanyPageCard from './account/CompanyPageCard'
import ContentProfileCard from './account/ContentProfileCard'
import NewsletterCard from './account/NewsletterCard'
import EngagementSettingsCard from './account/EngagementSettingsCard'
import GroupsCard from './account/GroupsCard'
import DmTemplatesCard from './account/DmTemplatesCard'
import LeadMagnetCard from './account/LeadMagnetCard'
import { SettingsSaveProvider, SaveAllBar } from './account/SettingsSaveContext'

type TabKey = 'account' | 'linkedin' | 'content' | 'automation'

const TABS: { key: TabKey; label: string }[] = [
  { key: 'account', label: 'Account & Billing' },
  { key: 'linkedin', label: 'LinkedIn Connection' },
  { key: 'content', label: 'Content & Profile' },
  { key: 'automation', label: 'Engagement & Automation' },
]

const VALID_TABS = TABS.map((t) => t.key) as string[]

export default function Account() {
  const queryClient = useQueryClient()
  const { data: readiness } = useAccountReadiness()
  const [searchParams, setSearchParams] = useSearchParams()
  const [oauthMsg, setOauthMsg] = useState<{ ok: boolean; text: string } | null>(null)

  const tabParam = searchParams.get('tab')
  const activeTab: TabKey = VALID_TABS.includes(tabParam ?? '') ? (tabParam as TabKey) : 'account'

  const setActiveTab = (tab: TabKey) => {
    setSearchParams(
      (prev) => {
        const next = new URLSearchParams(prev)
        next.set('tab', tab)
        return next
      },
      { replace: true }
    )
  }

  // Handle LinkedIn OAuth callback: ?li_connected=1 or ?li_error=... in URL
  useEffect(() => {
    const liConnected = searchParams.get('li_connected')
    const liError = searchParams.get('li_error')
    if (liConnected === '1') {
      localStorage.setItem('lem_li_connected', '1')
      // Force refetch so the fresh token is reflected immediately
      queryClient.invalidateQueries({ queryKey: ['token-status'] })
      setSearchParams({}, { replace: true })
      setOauthMsg({ ok: true, text: 'LinkedIn connected successfully!' })
      setTimeout(() => setOauthMsg(null), 5000)
    } else if (liError) {
      setOauthMsg({ ok: false, text: 'LinkedIn connection failed — please try again.' })
      setSearchParams({}, { replace: true })
      setTimeout(() => setOauthMsg(null), 8000)
    }
  }, [])

  return (
    <div className="max-w-2xl space-y-6">
      <h1 className="text-2xl font-bold text-gray-800">Account Settings</h1>

      {oauthMsg && (
        <p className={`text-sm font-medium ${oauthMsg.ok ? 'text-green-600' : 'text-red-600'}`}>
          {oauthMsg.text}
        </p>
      )}

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

      {/* Tab switcher */}
      <div className="flex flex-wrap gap-1 border-b border-gray-200">
        {TABS.map((t) => (
          <button
            key={t.key}
            type="button"
            onClick={() => setActiveTab(t.key)}
            className={`px-3 py-2 text-sm font-semibold border-b-2 -mb-px transition-colors ${
              activeTab === t.key
                ? 'border-blue-600 text-blue-700'
                : 'border-transparent text-gray-500 hover:text-gray-700 hover:border-gray-300'
            }`}
          >
            {t.label}
          </button>
        ))}
      </div>

      {activeTab === 'account' && (
        <div className="space-y-6">
          <SubscriptionCard />
          <VideoCreditsCard />
          <TimezoneCard />
          <LoginLocationCard />
          <InactivityCard />
        </div>
      )}

      {activeTab === 'linkedin' && (
        <div className="space-y-6">
          <LinkedInLoginCard />
          <CompanyPageCard />
        </div>
      )}

      {activeTab === 'content' && (
        <div className="space-y-6">
          <ContentProfileCard />
          <NewsletterCard />
        </div>
      )}

      {activeTab === 'automation' && (
        <SettingsSaveProvider>
          <div className="space-y-6">
            <EngagementSettingsCard />
            <GroupsCard />
            <DmTemplatesCard />
            <LeadMagnetCard />
          </div>
          <SaveAllBar />
        </SettingsSaveProvider>
      )}
    </div>
  )
}
