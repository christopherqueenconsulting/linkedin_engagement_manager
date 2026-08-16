import { useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import api from '../../api/client'
import { useAuth } from '../../contexts/useAuth'

export default function ContentProfileCard() {
  const { user, sessionToken } = useAuth()
  const queryClient = useQueryClient()
  const email = user?.email ?? ''
  // Edits in progress, once there are any — null means "show the DB value, or the cached one until
  // the DB answers". Derived rather than seeded in an effect so a refetch can never overwrite a
  // half-typed URL. localStorage is read once, as a placeholder only; the DB is the source of truth.
  const [blogEdit, setBlogEdit] = useState<string | null>(null)
  const [sitemapEdit, setSitemapEdit] = useState<string | null>(null)
  const [cached] = useState(() => ({
    blog: localStorage.getItem('lem_blog_url') || '',
    sitemap: localStorage.getItem('lem_sitemap_url') || '',
  }))
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

  const blogUrl = blogEdit ?? settingsData?.blog_url ?? cached.blog
  const sitemapUrl = sitemapEdit ?? settingsData?.sitemap_url ?? cached.sitemap

  // Mirror what the DB says into the placeholder cache — an external store, which is what an
  // effect is for. It is only ever read before the first /user/settings answer lands.
  useEffect(() => {
    if (settingsData?.blog_url) localStorage.setItem('lem_blog_url', settingsData.blog_url)
    if (settingsData?.sitemap_url) localStorage.setItem('lem_sitemap_url', settingsData.sitemap_url)
  }, [settingsData])

  const saveMutation = useMutation({
    mutationFn: () => {
      // Send only the URLs we can vouch for: one the user edited, or one whose stored value has
      // loaded. An omitted field is left alone by the endpoint, and an empty one is CLEARED — so
      // the localStorage placeholder must never be written, or a save made before /user/settings
      // answers wipes a stored URL with a stale hint (issue #1574).
      const body: { session_token: string | null; blog_url?: string | null; sitemap_url?: string | null } =
        { session_token: sessionToken }
      if (blogEdit !== null || settingsData) body.blog_url = (blogEdit ?? settingsData?.blog_url) || null
      if (sitemapEdit !== null || settingsData) body.sitemap_url = (sitemapEdit ?? settingsData?.sitemap_url) || null
      return api.put('/user/', body)
    },
    onSuccess: async () => {
      localStorage.setItem('lem_blog_url', blogUrl)
      localStorage.setItem('lem_sitemap_url', sitemapUrl)
      // Re-read the row this card and every other reader of /user/settings share. Without it the
      // cached pre-save row is served for the rest of its 60s staleTime, so coming back to the page
      // shows the OLD URL — a save that worked, reported as one that did not (issue #1574).
      await queryClient.invalidateQueries({ queryKey: ['user-settings'] })
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
          onChange={(e) => setBlogEdit(e.target.value)}
          className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
          placeholder="https://yourblog.com"
        />
      </div>

      <div>
        <label className="block text-sm font-medium text-gray-700 mb-1">Sitemap URL</label>
        <input
          type="url"
          value={sitemapUrl}
          onChange={(e) => setSitemapEdit(e.target.value)}
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
