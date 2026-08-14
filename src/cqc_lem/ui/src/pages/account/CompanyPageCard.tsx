import { useState } from 'react'
import { useMutation, useQuery } from '@tanstack/react-query'
import api from '../../api/client'
import { useAuth } from '../../contexts/useAuth'
import SettingsCard from '../../components/SettingsCard'

export default function CompanyPageCard() {
  const { sessionToken } = useAuth()
  // The edit in progress, once there is one — null means "show what the API returned". Derived
  // rather than seeded in an effect so a refetch can never overwrite what the user typed.
  const [companyPageEdit, setCompanyPageEdit] = useState<string | null>(null)
  const [companyPageMsg, setCompanyPageMsg] = useState<{ ok: boolean; text: string } | null>(null)

  const { data: settingsData } = useQuery({
    queryKey: ['user-settings', sessionToken],
    queryFn: () =>
      api
        .get(`/user/settings?session_token=${encodeURIComponent(sessionToken!)}`)
        .then((r) => r.data.detail as {
          subscription: {
            status: string | null
            tier: string | null
            trial_started_at: string | null
            trial_ends_at: string | null
            stripe_customer_id: string | null
          } | null
          preferences: {
            last_login_inactivate_delay: number | null
            auto_schedule_posts: boolean
          } | null
          blog_url: string | null
          sitemap_url: string | null
          company_linked_in_url: string | null
        }),
    enabled: !!sessionToken,
    staleTime: 60 * 1000,
  })

  const companyPage = companyPageEdit ?? settingsData?.company_linked_in_url ?? ''

  // LinkedIn company page save (used by the monthly company-page invite automation)
  const companyPageMutation = useMutation({
    mutationFn: () =>
      api.put('/user/company-page', {
        session_token: sessionToken,
        company_linked_in_url: companyPage.trim() || null,
      }),
    onSuccess: () => {
      setCompanyPageMsg({ ok: true, text: companyPage.trim() ? 'Company page saved.' : 'Company page cleared.' })
      setTimeout(() => setCompanyPageMsg(null), 4000)
    },
    onError: () => {
      setCompanyPageMsg({ ok: false, text: 'Save failed — use a full linkedin.com/company/… URL.' })
      setTimeout(() => setCompanyPageMsg(null), 6000)
    },
  })

  return (
    <SettingsCard
      title="LinkedIn Company Page"
      subtitle="If you have a LinkedIn company page, add it here. On the 1st of each month LEM invites your connections to follow it (using your monthly invite credits, which reset monthly). Leave blank if you don't have one."
    >
      <div>
        <label className="block text-sm font-medium text-gray-700 mb-1">Company page URL</label>
        <input
          type="url"
          value={companyPage}
          onChange={(e) => setCompanyPageEdit(e.target.value)}
          className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
          placeholder="https://www.linkedin.com/company/your-company/"
        />
      </div>

      {companyPageMsg && (
        <p className={`text-sm font-medium ${companyPageMsg.ok ? 'text-green-600' : 'text-red-600'}`}>
          {companyPageMsg.text}
        </p>
      )}

      <button
        type="button"
        onClick={() => companyPageMutation.mutate()}
        disabled={companyPageMutation.isPending}
        className="w-full bg-blue-600 text-white py-2 rounded-lg text-sm font-semibold hover:bg-blue-700 disabled:opacity-50 transition-colors"
      >
        {companyPageMutation.isPending ? 'Saving…' : 'Save Company Page'}
      </button>
    </SettingsCard>
  )
}
