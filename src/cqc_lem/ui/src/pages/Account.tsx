import { useState, useEffect } from 'react'
import { useSearchParams } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import api from '../api/client'
import { useAuth } from '../contexts/AuthContext'
import { useAccountReadiness } from '../hooks/useAccountReadiness'
import LinkedInSessionCard from '../components/LinkedInSessionCard'

const LI_AUTH_URL = '/api/auth/linkedin/'

const TIER_LABELS: Record<string, string> = {
  free_trial: 'Free Trial',
  starter: 'Starter',
  professional: 'Professional',
  enterprise: 'Enterprise',
}

const TIER_COLORS: Record<string, string> = {
  free_trial: 'bg-gray-100 text-gray-700',
  starter: 'bg-blue-100 text-blue-800',
  professional: 'bg-purple-100 text-purple-800',
  enterprise: 'bg-yellow-100 text-yellow-800',
}

const VIDEO_PACKAGES = [
  { key: 'small', price: '$12', label: '5 credits' },
  { key: 'medium', price: '$30', label: '15 credits' },
  { key: 'large', price: '$70', label: '40 credits' },
  { key: 'max', price: '$150', label: '100 credits' },
]

const COMMON_TIMEZONES = [
  'America/New_York',
  'America/Chicago',
  'America/Denver',
  'America/Los_Angeles',
  'America/Phoenix',
  'America/Anchorage',
  'Pacific/Honolulu',
  'America/Toronto',
  'America/Vancouver',
  'America/Sao_Paulo',
  'America/Mexico_City',
  'Europe/London',
  'Europe/Paris',
  'Europe/Berlin',
  'Europe/Amsterdam',
  'Europe/Rome',
  'Europe/Madrid',
  'Europe/Moscow',
  'Asia/Dubai',
  'Asia/Kolkata',
  'Asia/Singapore',
  'Asia/Tokyo',
  'Asia/Shanghai',
  'Australia/Sydney',
  'Pacific/Auckland',
  'UTC',
]

const INACTIVATE_OPTIONS = [
  { label: '30 days', value: 30 },
  { label: '60 days', value: 60 },
  { label: '90 days (default)', value: 90 },
  { label: '120 days', value: 120 },
  { label: '365 days', value: 365 },
  { label: 'Never', value: null },
]

function SectionHeading({ title }: { title: string }) {
  return (
    <h2 className="text-xs font-bold uppercase tracking-wide text-gray-400 pt-3 -mb-2">{title}</h2>
  )
}

function daysUntil(iso: string | null | undefined): number | null {
  if (!iso) return null
  const diff = new Date(iso).getTime() - Date.now()
  return Math.max(0, Math.floor(diff / (1000 * 60 * 60 * 24)))
}

export default function Account() {
  const { user, sessionToken } = useAuth()
  const email = user?.email ?? ''
  const queryClient = useQueryClient()
  const { data: readiness } = useAccountReadiness()
  const sessionOk = readiness?.items.find((i) => i.key === 'linkedin_session')?.ok ?? false

  const [searchParams, setSearchParams] = useSearchParams()
  const [blogUrl, setBlogUrl] = useState(localStorage.getItem('lem_blog_url') || '')
  const [sitemapUrl, setSitemapUrl] = useState(localStorage.getItem('lem_sitemap_url') || '')
  const [companyPage, setCompanyPage] = useState('')
  const [companyPageMsg, setCompanyPageMsg] = useState<{ ok: boolean; text: string } | null>(null)
  const [liConnectedLocal, setLiConnectedLocal] = useState(
    localStorage.getItem('lem_li_connected') === '1'
  )
  const [savedMsg, setSavedMsg] = useState<string | null>(null)
  const [prefsSavedMsg, setPrefsSavedMsg] = useState<string | null>(null)
  const [checkoutMsg, setCheckoutMsg] = useState<{ ok: boolean; text: string } | null>(null)
  const [liPassword, setLiPassword] = useState('')
  const [showLiPassword, setShowLiPassword] = useState(false)
  const [liPasswordMsg, setLiPasswordMsg] = useState<{ ok: boolean; text: string } | null>(null)

  // Preferences local state — initialised from query once loaded
  const [inactivateDelay, setInactivateDelay] = useState<number | null>(90)
  const [autoSchedule, setAutoSchedule] = useState(false)
  const [prefsInitialised, setPrefsInitialised] = useState(false)
  const [urlsInitialised, setUrlsInitialised] = useState(false)

  // Timezone state
  const [timezone, setTimezone] = useState('America/New_York')
  const [tzInitialised, setTzInitialised] = useState(false)
  const [tzSavedMsg, setTzSavedMsg] = useState<string | null>(null)
  const [locationMsg, setLocationMsg] = useState<{ ok: boolean; text: string } | null>(null)
  const [locCity, setLocCity] = useState('')
  const [locState, setLocState] = useState('')
  const [locCountry, setLocCountry] = useState('US')

  // Handle LinkedIn OAuth callback: ?li_connected=1 or ?li_error=... in URL
  useEffect(() => {
    const liConnected = searchParams.get('li_connected')
    const liError = searchParams.get('li_error')
    if (liConnected === '1') {
      localStorage.setItem('lem_li_connected', '1')
      setLiConnectedLocal(true)
      // Force refetch so the fresh token is reflected immediately
      queryClient.invalidateQueries({ queryKey: ['token-status'] })
      setSearchParams({}, { replace: true })
      setSavedMsg('LinkedIn connected successfully!')
      setTimeout(() => setSavedMsg(null), 5000)
    } else if (liError) {
      setSavedMsg('LinkedIn connection failed — please try again.')
      setSearchParams({}, { replace: true })
      setTimeout(() => setSavedMsg(null), 8000)
    }
    setBlogUrl(localStorage.getItem('lem_blog_url') || '')
    setSitemapUrl(localStorage.getItem('lem_sitemap_url') || '')
  }, [])

  // LinkedIn token status
  const { data: tokenStatusData } = useQuery({
    queryKey: ['token-status', sessionToken],
    queryFn: () =>
      api
        .get(`/user/token_status?session_token=${encodeURIComponent(sessionToken!)}`)
        .then((r) => r.data.detail as {
          token_expiry_date: string | null
          is_expiring_soon: boolean
          is_expired: boolean
          refresh_attempted: boolean
          refresh_succeeded: boolean
        }),
    enabled: !!sessionToken,
    staleTime: 5 * 60 * 1000,
  })

  // Subscription + preferences
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

  useEffect(() => {
    if (settingsData?.preferences && !prefsInitialised) {
      setInactivateDelay(settingsData.preferences.last_login_inactivate_delay)
      setAutoSchedule(settingsData.preferences.auto_schedule_posts)
      setPrefsInitialised(true)
    }
  }, [settingsData, prefsInitialised])

  // Timezone query
  const { data: tzData } = useQuery({
    queryKey: ['user-timezone', sessionToken],
    queryFn: () =>
      api
        .get(`/user/timezone?session_token=${encodeURIComponent(sessionToken!)}`)
        .then((r) => r.data.detail as { timezone: string }),
    enabled: !!sessionToken,
    staleTime: 5 * 60 * 1000,
  })

  const { data: videoCreditData } = useQuery({
    queryKey: ['video-credits', sessionToken],
    queryFn: () =>
      api
        .get('/video/credits', { params: { session_token: sessionToken } })
        .then((r) => r.data.detail as { balance: number }),
    enabled: !!sessionToken,
  })
  const videoCredits = videoCreditData?.balance ?? 0

  const buyVideoCreditsMutation = useMutation({
    mutationFn: async (pkg: string) => {
      const r = await api.post('/video/credits/checkout', {
        session_token: sessionToken,
        package: pkg,
        success_url: `${window.location.origin}/account?video_credits=purchased`,
        cancel_url: `${window.location.origin}/account`,
      })
      return r.data.detail.checkout_url as string
    },
    onSuccess: (url) => { window.location.href = url },
  })

  useEffect(() => {
    if (tzData?.timezone && !tzInitialised) {
      setTimezone(tzData.timezone)
      setTzInitialised(true)
    }
  }, [tzData, tzInitialised])

  // Seed blog/sitemap from DB on first load — DB is source of truth over localStorage
  useEffect(() => {
    if (settingsData && !urlsInitialised) {
      const apiBlogUrl = settingsData.blog_url ?? ''
      const apiSitemapUrl = settingsData.sitemap_url ?? ''
      if (apiBlogUrl) {
        setBlogUrl(apiBlogUrl)
        localStorage.setItem('lem_blog_url', apiBlogUrl)
      }
      if (apiSitemapUrl) {
        setSitemapUrl(apiSitemapUrl)
        localStorage.setItem('lem_sitemap_url', apiSitemapUrl)
      }
      if (settingsData.company_linked_in_url) {
        setCompanyPage(settingsData.company_linked_in_url)
      }
      setUrlsInitialised(true)
    }
  }, [settingsData, urlsInitialised])

  const subscription = settingsData?.subscription
  const tier = subscription?.tier ?? 'free_trial'
  const trialDays = daysUntil(subscription?.trial_ends_at)
  const isOnTrial = subscription?.status === 'trial'
  const isPaidPlan = subscription?.status === 'active'
  const hasStripeCustomer = !!subscription?.stripe_customer_id

  const hasValidToken = tokenStatusData ? !tokenStatusData.is_expired : false
  const isLinkedInConnected = liConnectedLocal || hasValidToken
  const tokenExpiringSoon = tokenStatusData?.is_expiring_soon ?? false
  const tokenExpired = tokenStatusData?.is_expired ?? false

  const showLinkedInSection = !isLinkedInConnected || tokenExpiringSoon || tokenExpired

  // Profile save
  const saveMutation = useMutation({
    mutationFn: () =>
      api.put('/user/', { email, blog_url: blogUrl || null, sitemap_url: sitemapUrl || null }),
    onSuccess: () => {
      localStorage.setItem('lem_blog_url', blogUrl)
      localStorage.setItem('lem_sitemap_url', sitemapUrl)
      setSavedMsg('Settings saved!')
      setTimeout(() => setSavedMsg(null), 3000)
    },
    onError: () => {
      setSavedMsg('Save failed — please try again.')
      setTimeout(() => setSavedMsg(null), 5000)
    },
  })

  // Preferences save
  const prefsMutation = useMutation({
    mutationFn: () =>
      api.put('/user/settings', {
        session_token: sessionToken,
        last_login_inactivate_delay: inactivateDelay,
        auto_schedule_posts: autoSchedule,
      }),
    onSuccess: () => {
      setPrefsSavedMsg('Preferences saved!')
      setTimeout(() => setPrefsSavedMsg(null), 3000)
    },
    onError: () => {
      setPrefsSavedMsg('Save failed — please try again.')
      setTimeout(() => setPrefsSavedMsg(null), 5000)
    },
  })

  const tzMutation = useMutation({
    mutationFn: () =>
      api.put('/user/timezone', { session_token: sessionToken, timezone }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['user-timezone'] })
      setTzSavedMsg('Timezone saved!')
      setTimeout(() => setTzSavedMsg(null), 3000)
    },
    onError: () => {
      setTzSavedMsg('Save failed — please try again.')
      setTimeout(() => setTzSavedMsg(null), 5000)
    },
  })

  // Login location — used so the automation browser appears to log in from where
  // you normally do, reducing LinkedIn "new location" challenges.
  const { data: locationData } = useQuery({
    queryKey: ['user-location', sessionToken],
    queryFn: () =>
      api
        .get(`/user/location?session_token=${encodeURIComponent(sessionToken!)}`)
        .then((r) => r.data.detail as {
          latitude?: number; longitude?: number; city?: string; country?: string; timezone?: string
        }),
    enabled: !!sessionToken,
  })

  const autocaptureLocationMutation = useMutation({
    mutationFn: () =>
      api
        .post('/user/location/autocapture', { session_token: sessionToken })
        .then((r) => r.data.detail),
    onSuccess: (d: { city?: string; country?: string }) => {
      queryClient.invalidateQueries({ queryKey: ['user-location'] })
      queryClient.invalidateQueries({ queryKey: ['user-timezone'] })
      const where = [d?.city, d?.country].filter(Boolean).join(', ')
      setLocationMsg({ ok: true, text: where ? `Location set to ${where}.` : 'Location detected.' })
      setTimeout(() => setLocationMsg(null), 4000)
    },
    onError: () => {
      setLocationMsg({ ok: false, text: 'Could not detect your location — try again.' })
      setTimeout(() => setLocationMsg(null), 5000)
    },
  })

  const setLocationByCityMutation = useMutation({
    mutationFn: () =>
      api
        .post('/user/location/by-city', {
          session_token: sessionToken,
          city: locCity.trim(),
          state: locState.trim() || null,
          country: locCountry.trim() || null,
        })
        .then((r) => r.data.detail),
    onSuccess: (d: { city?: string; country?: string; timezone?: string }) => {
      queryClient.invalidateQueries({ queryKey: ['user-location'] })
      queryClient.invalidateQueries({ queryKey: ['user-timezone'] })
      const where = [d?.city, d?.country].filter(Boolean).join(', ')
      setLocationMsg({ ok: true, text: `Location set to ${where}${d?.timezone ? ` (${d.timezone})` : ''}.` })
      setTimeout(() => setLocationMsg(null), 4000)
    },
    onError: (e: any) => {
      const detail = e?.response?.data?.detail
      setLocationMsg({ ok: false, text: typeof detail === 'string' ? detail : 'Could not set that location — check the city/state.' })
      setTimeout(() => setLocationMsg(null), 5000)
    },
  })

  // Stripe checkout redirect
  const checkoutMutation = useMutation({
    mutationFn: (tier: string) =>
      api
        .post('/billing/create-checkout-session', {
          session_token: sessionToken,
          tier,
          success_url: `${window.location.origin}/account?upgraded=1`,
          cancel_url: `${window.location.origin}/account`,
        })
        .then((r) => r.data.detail as { checkout_url: string | null; upgraded?: boolean }),
    onSuccess: (detail) => {
      if (detail.upgraded || !detail.checkout_url) {
        // In-place upgrade — no Stripe redirect needed. Refresh subscription data.
        setCheckoutMsg({ ok: true, text: 'Plan updated successfully!' })
        queryClient.invalidateQueries({ queryKey: ['userSettings'] })
        setTimeout(() => setCheckoutMsg(null), 5000)
      } else {
        window.location.href = detail.checkout_url
      }
    },
    onError: () => {
      setCheckoutMsg({ ok: false, text: 'Could not start checkout — please try again.' })
      setTimeout(() => setCheckoutMsg(null), 6000)
    },
  })

  // LinkedIn password save
  const liPasswordMutation = useMutation({
    mutationFn: () =>
      api.put('/user/linkedin-password', {
        session_token: sessionToken,
        linkedin_password: liPassword,
      }),
    onSuccess: () => {
      setLiPassword('')
      setLiPasswordMsg({ ok: true, text: 'LinkedIn password saved.' })
      setTimeout(() => setLiPasswordMsg(null), 4000)
    },
    onError: () => {
      setLiPasswordMsg({ ok: false, text: 'Save failed — please try again.' })
      setTimeout(() => setLiPasswordMsg(null), 5000)
    },
  })

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

  // Stripe portal redirect
  const portalMutation = useMutation({
    mutationFn: () =>
      api
        .post('/billing/create-portal-session', {
          session_token: sessionToken,
          return_url: `${window.location.origin}/account`,
        })
        .then((r) => r.data.detail.portal_url as string),
    onSuccess: (url) => { window.location.href = url },
    onError: () => setSavedMsg('Could not open billing portal — please try again.'),
  })

  function handleSave(e: React.FormEvent) {
    e.preventDefault()
    saveMutation.mutate()
  }

  function handlePrefsSave(e: React.FormEvent) {
    e.preventDefault()
    prefsMutation.mutate()
  }

  return (
    <div className="max-w-2xl space-y-6">
      <h1 className="text-2xl font-bold text-gray-800">Account Settings</h1>

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

      <SectionHeading title="Plan & Billing" />

      {/* Subscription card */}
      <div className="bg-white rounded-lg shadow-sm border border-gray-200 p-6 space-y-4">
        <div className="flex items-center justify-between">
          <h2 className="text-base font-semibold text-gray-700">
            Subscription <span className="text-red-500">*</span>
          </h2>
          <span className={`text-xs font-semibold px-2.5 py-1 rounded-full ${TIER_COLORS[tier] ?? TIER_COLORS.free_trial}`}>
            {TIER_LABELS[tier] ?? tier}
          </span>
        </div>

        {isOnTrial && trialDays !== null && (
          <div className={`rounded-lg p-3 text-sm ${trialDays <= 3 ? 'bg-red-50 text-red-800 border border-red-200' : 'bg-yellow-50 text-yellow-800 border border-yellow-200'}`}>
            {trialDays === 0
              ? 'Your free trial has expired. Upgrade to keep using LEM.'
              : `Free trial: ${trialDays} day${trialDays === 1 ? '' : 's'} remaining.`}
          </div>
        )}

        {isPaidPlan && (
          <p className="text-sm text-green-700">Your subscription is active.</p>
        )}

        {/* Upgrade options — shown to trial users without a paid plan */}
        {!isPaidPlan && hasStripeCustomer && (
          <div className="space-y-2">
            <p className="text-xs text-gray-500 font-medium uppercase tracking-wide">Upgrade your plan</p>
            <div className="grid grid-cols-3 gap-2">
              {[
                { tier: 'starter', label: 'Starter', price: '$29/mo' },
                { tier: 'professional', label: 'Professional', price: '$79/mo' },
                { tier: 'enterprise', label: 'Enterprise', price: '$199/mo' },
              ].map(({ tier: t, label, price }) => (
                <button
                  key={t}
                  onClick={() => checkoutMutation.mutate(t)}
                  disabled={checkoutMutation.isPending}
                  className="flex flex-col items-center border border-blue-200 rounded-lg p-3 hover:bg-blue-50 transition-colors disabled:opacity-50 text-center"
                >
                  <span className="text-sm font-semibold text-blue-800">{label}</span>
                  <span className="text-xs text-gray-500">{price}</span>
                </button>
              ))}
            </div>
            {checkoutMutation.isPending && (
              <p className="text-xs text-blue-600">Redirecting to checkout…</p>
            )}
            {checkoutMsg && (
              <p className={`text-sm font-medium ${checkoutMsg.ok ? 'text-green-600' : 'text-red-600'}`}>
                {checkoutMsg.text}
              </p>
            )}
          </div>
        )}

        {/* Billing portal — shown to paid subscribers */}
        {isPaidPlan && hasStripeCustomer && (
          <button
            onClick={() => portalMutation.mutate()}
            disabled={portalMutation.isPending}
            className="text-sm text-blue-600 hover:underline disabled:opacity-50"
          >
            Manage billing →
          </button>
        )}

        {!hasStripeCustomer && (
          <p className="text-xs text-gray-400">
            Billing not yet configured for your account. Contact support if you need help.
          </p>
        )}
      </div>

      {/* Premium video credits */}
      <div className="bg-white rounded-lg shadow-sm border border-gray-200 p-6 space-y-4">
        <div className="flex items-center justify-between">
          <h2 className="text-base font-semibold text-gray-700">Premium Video Credits</h2>
          <span className="text-2xl font-bold text-blue-900" data-testid="video-credit-balance">{videoCredits}</span>
        </div>
        <p className="text-sm text-gray-500">
          Premium videos use Google Veo (realistic motion + native audio): 1 credit per premium video,
          3 for top realism. Standard videos are always free with your plan.
        </p>
        <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
          {VIDEO_PACKAGES.map((pkg) => (
            <div
              key={pkg.key}
              className={`relative border rounded-xl p-4 flex flex-col items-center gap-1 ${
                pkg.key === 'medium' ? 'border-blue-400 bg-blue-50' : 'border-gray-200 bg-white'
              }`}
            >
              <span className="text-xl font-bold text-gray-900">{pkg.price}</span>
              <span className="text-xs font-medium text-gray-600 text-center">{pkg.label}</span>
              <button
                onClick={() => buyVideoCreditsMutation.mutate(pkg.key)}
                disabled={buyVideoCreditsMutation.isPending}
                className="mt-2 w-full py-1.5 rounded-lg bg-blue-600 hover:bg-blue-700 text-white text-sm font-medium disabled:opacity-50 transition-colors"
              >
                {buyVideoCreditsMutation.isPending ? '…' : 'Buy'}
              </button>
            </div>
          ))}
        </div>
      </div>

      <SectionHeading title="LinkedIn" />

      {/* LinkedIn connection — hidden when connected and token healthy */}
      {showLinkedInSection && (
        <div className="bg-white rounded-lg shadow-sm border border-gray-200 p-6 space-y-4">
          <h2 className="text-base font-semibold text-gray-700">
            LinkedIn Connection <span className="text-red-500">*</span>
            <span className="ml-2 text-[11px] font-semibold text-red-600 bg-red-50 px-2 py-0.5 rounded">
              Required
            </span>
          </h2>

          {isLinkedInConnected && tokenExpiringSoon && !tokenExpired && (
            <div className="bg-yellow-50 border border-yellow-200 rounded-lg p-3 text-sm text-yellow-800">
              Your LinkedIn token is expiring soon. Please reconnect to keep posting.
              {tokenStatusData?.refresh_attempted && !tokenStatusData?.refresh_succeeded && (
                <span className="ml-1 text-xs">(Auto-refresh failed — manual reconnect required.)</span>
              )}
            </div>
          )}

          {tokenExpired && isLinkedInConnected && (
            <div className="bg-red-50 border border-red-200 rounded-lg p-3 text-sm text-red-800">
              Your LinkedIn connection has expired. Please reconnect to continue posting.
            </div>
          )}

          <div className="flex items-center gap-3">
            <div
              className={`w-3 h-3 rounded-full flex-shrink-0 ${
                isLinkedInConnected && !tokenExpired ? 'bg-green-500' : 'bg-gray-300'
              }`}
            />
            <span className="text-sm text-gray-600">
              {isLinkedInConnected && !tokenExpired ? 'Connected to LinkedIn' : 'Not connected'}
            </span>
          </div>

          <p className="text-xs text-gray-500">
            Connecting allows LEM to post on your behalf, send DMs, reply to comments, and engage with your network automatically.
          </p>

          <a
            href={`${LI_AUTH_URL}?email=${encodeURIComponent(email)}&session_token=${encodeURIComponent(sessionToken ?? '')}`}
            className="inline-block w-full text-center py-2 rounded-lg text-sm font-semibold bg-blue-700 text-white hover:bg-blue-800 cursor-pointer transition-colors"
          >
            {isLinkedInConnected ? 'Reconnect LinkedIn' : 'Connect LinkedIn'}
          </a>
        </div>
      )}

      {/* LinkedIn Session (cookie) — preferred login method; avoids the device challenge */}
      <LinkedInSessionCard connected={sessionOk} />

      {/* LinkedIn Company Page — drives the monthly connection-invite automation */}
      <div className="bg-white rounded-lg shadow-sm border border-gray-200 p-6 space-y-4">
        <div>
          <h2 className="text-base font-semibold text-gray-700">LinkedIn Company Page</h2>
          <p className="text-xs text-gray-500 mt-1">
            If you have a LinkedIn company page, add it here. On the 1st of each month LEM
            invites your connections to follow it (using your monthly invite credits, which
            reset monthly). Leave blank if you don't have one.
          </p>
        </div>

        <div>
          <label className="block text-sm font-medium text-gray-700 mb-1">Company page URL</label>
          <input
            type="url"
            value={companyPage}
            onChange={(e) => setCompanyPage(e.target.value)}
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
      </div>

      {/* LinkedIn Automation Password — always shown when LinkedIn is connected */}
      {isLinkedInConnected && !tokenExpired && (
        <div className="bg-white rounded-lg shadow-sm border border-gray-200 p-6 space-y-4">
          <div>
            <h2 className="text-base font-semibold text-gray-700">LinkedIn Automation Password</h2>
            <p className="text-xs text-gray-500 mt-1">
              LEM uses your LinkedIn password to log in via browser automation for actions like
              profile scraping, post personalization, and DM engagement. It is stored securely and
              never shared or displayed after saving.
            </p>
          </div>

          <div>
            <label className="block text-sm font-medium text-gray-700 mb-1">LinkedIn Password</label>
            <div className="relative">
              <input
                type={showLiPassword ? 'text' : 'password'}
                value={liPassword}
                onChange={(e) => setLiPassword(e.target.value)}
                className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm pr-16 focus:outline-none focus:ring-2 focus:ring-blue-500"
                placeholder="Enter your LinkedIn password"
                autoComplete="new-password"
              />
              <button
                type="button"
                onClick={() => setShowLiPassword((v) => !v)}
                className="absolute inset-y-0 right-2 flex items-center px-2 text-xs text-gray-500 hover:text-gray-700"
              >
                {showLiPassword ? 'Hide' : 'Show'}
              </button>
            </div>
          </div>

          {liPasswordMsg && (
            <p className={`text-sm font-medium ${liPasswordMsg.ok ? 'text-green-600' : 'text-red-600'}`}>
              {liPasswordMsg.text}
            </p>
          )}

          <button
            type="button"
            onClick={() => liPasswordMutation.mutate()}
            disabled={liPasswordMutation.isPending || !liPassword}
            className="w-full bg-blue-600 text-white py-2 rounded-lg text-sm font-semibold hover:bg-blue-700 disabled:opacity-50 transition-colors"
          >
            {liPasswordMutation.isPending ? 'Saving…' : 'Save Password'}
          </button>
        </div>
      )}

      <SectionHeading title="Content & Profile" />

      {/* Email + content sources the AI uses for post ideas */}
      <form onSubmit={handleSave} className="bg-white rounded-lg shadow-sm border border-gray-200 p-6 space-y-4">
        <h2 className="text-base font-semibold text-gray-700">Content &amp; Profile</h2>

        <div>
          <label className="block text-sm font-medium text-gray-700 mb-1">Email</label>
          <p className="text-sm text-gray-800 bg-gray-50 border border-gray-200 rounded-lg px-3 py-2">
            {email}
          </p>
          <p className="text-xs text-green-600 mt-1">✓ Verified email</p>
        </div>

        <div>
          <label className="block text-sm font-medium text-gray-700 mb-1">Blog URL</label>
          <input
            type="url"
            value={blogUrl}
            onChange={(e) => setBlogUrl(e.target.value)}
            className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
            placeholder="https://yourblog.com"
          />
        </div>

        <div>
          <label className="block text-sm font-medium text-gray-700 mb-1">Sitemap URL</label>
          <input
            type="url"
            value={sitemapUrl}
            onChange={(e) => setSitemapUrl(e.target.value)}
            className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
            placeholder="https://yourblog.com/sitemap.xml"
          />
          <p className="text-xs text-gray-400 mt-1">Used by AI to generate content ideas from your existing posts.</p>
        </div>

        {savedMsg && (
          <p className={`text-sm font-medium ${saveMutation.isError ? 'text-red-600' : 'text-green-600'}`}>
            {savedMsg}
          </p>
        )}

        <button
          type="submit"
          disabled={saveMutation.isPending}
          className="w-full bg-blue-600 text-white py-2 rounded-lg text-sm font-semibold hover:bg-blue-700 disabled:opacity-50 transition-colors"
        >
          {saveMutation.isPending ? 'Saving…' : 'Save'}
        </button>
      </form>

      <SectionHeading title="Automation Settings" />

      {/* Preferences card */}
      <form onSubmit={handlePrefsSave} className="bg-white rounded-lg shadow-sm border border-gray-200 p-6 space-y-5">
        <h2 className="text-base font-semibold text-gray-700">Preferences</h2>

        <div>
          <label className="block text-sm font-medium text-gray-700 mb-1">
            Inactivity auto-stop delay
          </label>
          <select
            value={inactivateDelay === null ? 'never' : String(inactivateDelay)}
            onChange={(e) =>
              setInactivateDelay(e.target.value === 'never' ? null : Number(e.target.value))
            }
            className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
          >
            {INACTIVATE_OPTIONS.map(({ label, value }) => (
              <option key={String(value)} value={value === null ? 'never' : String(value)}>
                {label}
              </option>
            ))}
          </select>
          <p className="text-xs text-gray-400 mt-1">
            If you haven't logged in within this window, LEM will pause automated posting and LinkedIn activity to avoid acting on your behalf unintentionally.
            Set to "Never" to keep automation running regardless of login activity.
          </p>
        </div>

        <div>
          <div className="flex items-center justify-between">
            <div>
              <p className="text-sm font-medium text-gray-700">Auto-schedule AI posts</p>
              <p className="text-xs text-gray-400 mt-0.5">
                When on, AI-generated posts are automatically approved and queued for posting.
                When off, each post waits for your manual approval in the Review tab.
              </p>
            </div>
            <button
              type="button"
              onClick={() => setAutoSchedule((v) => !v)}
              className={`relative inline-flex h-6 w-11 flex-shrink-0 cursor-pointer rounded-full border-2 border-transparent transition-colors duration-200 focus:outline-none focus:ring-2 focus:ring-blue-500 focus:ring-offset-2 ${
                autoSchedule ? 'bg-blue-600' : 'bg-gray-200'
              }`}
              role="switch"
              aria-checked={autoSchedule}
            >
              <span
                className={`pointer-events-none inline-block h-5 w-5 transform rounded-full bg-white shadow ring-0 transition duration-200 ease-in-out ${
                  autoSchedule ? 'translate-x-5' : 'translate-x-0'
                }`}
              />
            </button>
          </div>
        </div>

        {prefsSavedMsg && (
          <p className={`text-sm font-medium ${prefsMutation.isError ? 'text-red-600' : 'text-green-600'}`}>
            {prefsSavedMsg}
          </p>
        )}

        <button
          type="submit"
          disabled={prefsMutation.isPending}
          className="w-full bg-blue-600 text-white py-2 rounded-lg text-sm font-semibold hover:bg-blue-700 disabled:opacity-50 transition-colors"
        >
          {prefsMutation.isPending ? 'Saving…' : 'Save Preferences'}
        </button>
      </form>

      {/* Timezone card */}
      <form
        onSubmit={(e) => { e.preventDefault(); tzMutation.mutate() }}
        className="bg-white rounded-lg shadow-sm border border-gray-200 p-6 space-y-4"
      >
        <h2 className="text-base font-semibold text-gray-700">Timezone</h2>
        <div>
          <label className="block text-sm font-medium text-gray-700 mb-1">Your timezone</label>
          <select
            value={timezone}
            onChange={(e) => setTimezone(e.target.value)}
            className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
          >
            {COMMON_TIMEZONES.map((tz) => (
              <option key={tz} value={tz}>{tz}</option>
            ))}
          </select>
          <p className="text-xs text-gray-400 mt-1">
            Scheduled post times will be displayed in this timezone across all pages.
          </p>
        </div>

        {tzSavedMsg && (
          <p className={`text-sm font-medium ${tzMutation.isError ? 'text-red-600' : 'text-green-600'}`}>
            {tzSavedMsg}
          </p>
        )}

        <button
          type="submit"
          disabled={tzMutation.isPending}
          className="w-full bg-blue-600 text-white py-2 rounded-lg text-sm font-semibold hover:bg-blue-700 disabled:opacity-50 transition-colors"
        >
          {tzMutation.isPending ? 'Saving…' : 'Save Timezone'}
        </button>
      </form>

      {/* Login location card */}
      <div className="bg-white rounded-lg shadow-sm border border-gray-200 p-6 space-y-4">
        <h2 className="text-base font-semibold text-gray-700">Login Location</h2>
        <p className="text-xs text-gray-500">
          The automation logs into LinkedIn from our servers. Setting your location makes the
          browser appear to come from where you normally log in, which reduces LinkedIn
          "new location" security challenges.
        </p>

        <div className="text-sm text-gray-700">
          <span className="font-medium text-gray-600">Current: </span>
          {locationData?.latitude != null
            ? `${[locationData.city, locationData.country].filter(Boolean).join(', ') || 'Set'} (${locationData.latitude.toFixed(3)}, ${locationData.longitude?.toFixed(3)})`
            : 'Not set'}
        </div>

        {locationMsg && (
          <p className={`text-sm font-medium ${locationMsg.ok ? 'text-green-600' : 'text-red-600'}`}>
            {locationMsg.text}
          </p>
        )}

        {/* Manual city/state entry */}
        <div className="grid grid-cols-1 sm:grid-cols-3 gap-2">
          <input
            type="text"
            value={locCity}
            onChange={(e) => setLocCity(e.target.value)}
            placeholder="City"
            className="border border-gray-300 rounded-lg px-3 py-2 text-sm sm:col-span-1"
          />
          <input
            type="text"
            value={locState}
            onChange={(e) => setLocState(e.target.value)}
            placeholder="State / region"
            className="border border-gray-300 rounded-lg px-3 py-2 text-sm sm:col-span-1"
          />
          <input
            type="text"
            value={locCountry}
            onChange={(e) => setLocCountry(e.target.value)}
            placeholder="Country (ISO-2)"
            maxLength={2}
            className="border border-gray-300 rounded-lg px-3 py-2 text-sm uppercase sm:col-span-1"
          />
        </div>
        <button
          type="button"
          onClick={() => setLocationByCityMutation.mutate()}
          disabled={setLocationByCityMutation.isPending || !locCity.trim()}
          className="w-full bg-blue-600 text-white py-2 rounded-lg text-sm font-semibold hover:bg-blue-700 disabled:opacity-50 transition-colors"
        >
          {setLocationByCityMutation.isPending ? 'Setting…' : 'Set location'}
        </button>

        <div className="relative py-1">
          <div className="border-t border-gray-200" />
          <span className="absolute inset-0 -top-2 flex justify-center text-xs text-gray-400">
            <span className="bg-white px-2">or</span>
          </span>
        </div>

        <button
          type="button"
          onClick={() => autocaptureLocationMutation.mutate()}
          disabled={autocaptureLocationMutation.isPending}
          className="w-full bg-gray-100 text-gray-700 py-2 rounded-lg text-sm font-semibold hover:bg-gray-200 disabled:opacity-50 transition-colors"
        >
          {autocaptureLocationMutation.isPending ? 'Detecting…' : 'Use my current location (auto-detect)'}
        </button>
      </div>
    </div>
  )
}
