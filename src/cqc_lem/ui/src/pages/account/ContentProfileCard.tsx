import { useEffect, useState } from 'react'
import { useMutation, useQuery } from '@tanstack/react-query'
import api from '../../api/client'
import { useAuth } from '../../contexts/AuthContext'

export default function ContentProfileCard() {
  const { user, sessionToken } = useAuth()
  const email = user?.email ?? ''
  const [blogUrl, setBlogUrl] = useState(localStorage.getItem('lem_blog_url') || '')
  const [sitemapUrl, setSitemapUrl] = useState(localStorage.getItem('lem_sitemap_url') || '')
  const [urlsInitialised, setUrlsInitialised] = useState(false)
  const [savedMsg, setSavedMsg] = useState<string | null>(null)

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
      setUrlsInitialised(true)
    }
  }, [settingsData, urlsInitialised])

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

  function handleSave(e: React.FormEvent) {
    e.preventDefault()
    saveMutation.mutate()
  }

  return (
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
  )
}
