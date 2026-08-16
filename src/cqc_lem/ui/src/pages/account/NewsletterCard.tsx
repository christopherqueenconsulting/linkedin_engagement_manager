import { useState } from 'react'
import { Link } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import api from '../../api/client'
import { useAuth } from '../../contexts/useAuth'
import Toggle from '../../components/Toggle'
import type { NewsletterSettings, NewsletterSubscribers } from './types'
import { WEEKDAYS } from './types'
import { FIELD_LIMITS } from './fieldLimits'
import { formatHour12 } from '../../utils/datetime'

export default function NewsletterCard() {
  const { sessionToken } = useAuth()
  const queryClient = useQueryClient()
  // Only the fields the user has actually touched. The card renders the API row with those edits
  // laid over it, so nothing has to be seeded in an effect and a refetch cannot discard an edit.
  const [nlEdit, setNlEdit] = useState<Partial<NewsletterSettings> | null>(null)
  const [nlMsg, setNlMsg] = useState<{ ok: boolean; text: string } | null>(null)

  const { data: nlData } = useQuery({
    queryKey: ['newsletter-settings', sessionToken],
    queryFn: () =>
      api
        .get(`/user/newsletter-settings?session_token=${encodeURIComponent(sessionToken!)}`)
        .then((r) => r.data.detail as NewsletterSettings),
    enabled: !!sessionToken,
    staleTime: 60 * 1000,
  })
  const newsletter: NewsletterSettings | null = nlData ? { ...nlData, ...nlEdit } : null

  const { data: subs } = useQuery({
    queryKey: ['newsletter-subscribers', sessionToken],
    queryFn: () =>
      api
        .get(`/user/newsletter-subscribers?session_token=${encodeURIComponent(sessionToken!)}`)
        .then((r) => r.data.detail as NewsletterSubscribers),
    enabled: !!sessionToken && !!newsletter?.enabled,
    staleTime: 60 * 1000,
  })

  const setNl = (patch: Partial<NewsletterSettings>) => setNlEdit((p) => ({ ...p, ...patch }))

  const nlMutation = useMutation({
    mutationFn: () => api.put('/user/newsletter-settings', { session_token: sessionToken, ...newsletter }),
    onSuccess: async () => {
      // Wait for the refetch: React Query serves the pre-save row until it lands, so dropping the
      // edits first would flash the old schedule back over what was just saved.
      await queryClient.invalidateQueries({ queryKey: ['newsletter-settings'] })
      setNlEdit(null)
      setNlMsg({ ok: true, text: 'Saved.' })
      setTimeout(() => setNlMsg(null), 3000)
    },
    onError: () => {
      setNlMsg({ ok: false, text: 'Could not save — try again.' })
      setTimeout(() => setNlMsg(null), 5000)
    },
  })

  if (!newsletter) return null

  return (
    <div className="bg-white rounded-lg shadow-sm border border-gray-200 p-6 space-y-4">
      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-base font-semibold text-gray-700">LinkedIn Newsletter</h2>
          <p className="text-xs text-gray-500">Auto-publish a recurring newsletter (bypasses the feed — subscribers get a notification + email). Repurposes your blog when set.</p>
        </div>
        <Toggle on={newsletter.enabled} onClick={() => setNl({ enabled: !newsletter.enabled })} />
      </div>
      {newsletter.enabled && (
        <>
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
            <div>
              <label className="block text-sm font-medium text-gray-700 mb-1">Title</label>
              <input type="text" value={newsletter.title || ''} onChange={(e) => setNl({ title: e.target.value })}
                maxLength={FIELD_LIMITS.nl_title}
                placeholder="e.g. The Growth Brief" className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm" />
            </div>
            <div>
              <label className="block text-sm font-medium text-gray-700 mb-1">Cadence</label>
              <select value={newsletter.cadence} onChange={(e) => setNl({ cadence: e.target.value })}
                className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm">
                <option value="weekly">Weekly (recommended)</option>
                <option value="biweekly">Bi-weekly</option>
                <option value="monthly">Monthly</option>
              </select>
            </div>
          </div>
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
            <div>
              <label className="block text-sm font-medium text-gray-700 mb-1">Publish day</label>
              <select value={newsletter.publish_day} onChange={(e) => setNl({ publish_day: Number(e.target.value) })}
                className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm">
                {WEEKDAYS.map((d, i) => (
                  <option key={i} value={i}>{d}</option>
                ))}
              </select>
            </div>
            <div>
              <label className="block text-sm font-medium text-gray-700 mb-1">Publish hour (your time)</label>
              <select value={newsletter.publish_hour} onChange={(e) => setNl({ publish_hour: Number(e.target.value) })}
                className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm">
                {Array.from({ length: 24 }, (_, h) => (
                  <option key={h} value={h}>{formatHour12(h)}</option>
                ))}
              </select>
            </div>
          </div>
          <div>
            <label className="block text-sm font-medium text-gray-700 mb-1">Topic / theme</label>
            <input type="text" value={newsletter.topic || ''} onChange={(e) => setNl({ topic: e.target.value })}
              maxLength={FIELD_LIMITS.nl_topic}
              placeholder="What each edition should focus on" className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm" />
          </div>

          {/* Plan-ahead controls: keep several drafts queued, generated early, so reviews never feel rushed. */}
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
            <div>
              <label className="block text-sm font-medium text-gray-700 mb-1">Draft newsletters to keep ready</label>
              <select value={newsletter.max_queued_drafts} onChange={(e) => setNl({ max_queued_drafts: Number(e.target.value) })}
                className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm">
                {Array.from({ length: 10 }, (_, i) => i + 1).map((n) => (
                  <option key={n} value={n}>{n}</option>
                ))}
              </select>
              <p className="text-xs text-gray-400 mt-1">We'll keep this many drafts queued so you can plan ahead.</p>
            </div>
            <div>
              <label className="block text-sm font-medium text-gray-700 mb-1">Generate drafts this many days ahead</label>
              <input type="number" min={0} max={60} value={newsletter.generate_lead_days}
                onChange={(e) => setNl({ generate_lead_days: Number(e.target.value) })}
                className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm" />
              <p className="text-xs text-gray-400 mt-1">How early each new draft appears for review.</p>
            </div>
          </div>

          {/* Autonomous publishing (issue #1135): a generated edition rests at 'draft', so without
              this on, its slot arrives and it waits for you rather than shipping on silence. */}
          <div className="border-t border-gray-100 pt-4 flex items-center justify-between">
            <div>
              <p className="text-sm font-medium text-gray-700">Publish drafts without my approval</p>
              <p className="text-xs text-gray-500">
                When this is on, a queued draft publishes at its scheduled slot on its own. When it's
                off, an edition only publishes after you approve it on the Newsletters review tab —
                its slot passes and the draft keeps waiting.
              </p>
            </div>
            <Toggle on={newsletter.auto_publish_newsletters} ariaLabel="Publish drafts without my approval"
              onClick={() => setNl({ auto_publish_newsletters: !newsletter.auto_publish_newsletters })} />
          </div>

          {/* Cover images (issue #893): upload your own per edition, or let us generate one.
              Generation costs money per edition, so it is opt-in and every generated cover waits
              for your approval before it publishes. */}
          <div className="border-t border-gray-100 pt-4 space-y-3">
            <div className="flex items-center justify-between">
              <div>
                <p className="text-sm font-medium text-gray-700">Auto-generate a cover image</p>
                <p className="text-xs text-gray-500">
                  Opt-in. We'll create a cover for each new draft using engagement best practices —
                  one clear subject, no text or logos. Every generated cover waits for your approval
                  before it publishes.
                </p>
              </div>
              <Toggle on={newsletter.cover_image_auto}
                onClick={() => setNl({ cover_image_auto: !newsletter.cover_image_auto })} />
            </div>
            <p className="text-xs text-gray-400">
              You can also upload your own cover for any edition on the{' '}
              <Link to="/content?tab=newsletters" className="text-blue-600 font-medium hover:underline">Newsletters</Link>{' '}
              review tab.
            </p>
          </div>

          {/* Subscriber growth: count over time + opt-in invite flow (capped). */}
          <div className="border-t border-gray-100 pt-4 space-y-3">
            <div className="flex items-center justify-between">
              <p className="text-sm font-medium text-gray-700">Subscribers</p>
              <p className="text-sm font-semibold text-gray-900">
                {subs?.latest != null ? subs.latest.toLocaleString() : '—'}
              </p>
            </div>
            {subs && subs.history.length > 0 && (
              <p className="text-xs text-gray-400">
                Tracked {subs.history.length} time{subs.history.length === 1 ? '' : 's'}; last invited{' '}
                {subs.history[0].invites_sent} connection{subs.history[0].invites_sent === 1 ? '' : 's'}.
              </p>
            )}
            {subs?.attribution && (
              <p className="text-xs text-gray-400">
                Owned-asset CTAs in the last {subs.attribution.window_days} days:{' '}
                {subs.attribution.newsletter_links ?? '—'} post
                {subs.attribution.newsletter_links === 1 ? '' : 's'} carried your subscribe link,{' '}
                {subs.attribution.lead_magnet_dms} lead-magnet DM
                {subs.attribution.lead_magnet_dms === 1 ? '' : 's'} queued for your approval.
              </p>
            )}
            <div className="flex items-center justify-between">
              <div>
                <p className="text-sm font-medium text-gray-700">Invite my connections to subscribe</p>
                <p className="text-xs text-gray-500">Opt-in. We only invite when this is on, up to your cap per run (plus LinkedIn's own weekly limit).</p>
              </div>
              <Toggle on={newsletter.invite_connections_enabled}
                onClick={() => setNl({ invite_connections_enabled: !newsletter.invite_connections_enabled })} />
            </div>
            {newsletter.invite_connections_enabled && (
              <div>
                <label className="block text-sm font-medium text-gray-700 mb-1">Max invites per run</label>
                <input type="number" min={0} max={500} value={newsletter.max_invites_per_run}
                  onChange={(e) => setNl({ max_invites_per_run: Number(e.target.value) })}
                  className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm" />
                <p className="text-xs text-gray-400 mt-1">Hard cap on connections invited each run (0–500).</p>
              </div>
            )}
          </div>

          <div className="flex items-center justify-between">
            <p className="text-sm font-medium text-gray-700">Align with my blog</p>
            <Toggle on={newsletter.align_with_blog} onClick={() => setNl({ align_with_blog: !newsletter.align_with_blog })} />
          </div>
          <p className="text-xs text-gray-500">
            Review &amp; approve drafts on the{' '}
            <Link to="/content?tab=newsletters" className="text-blue-600 font-medium hover:underline">Content → Newsletters</Link>{' '}
            tab.
          </p>
        </>
      )}
      {nlMsg && <p className={`text-sm font-medium ${nlMsg.ok ? 'text-green-600' : 'text-red-600'}`}>{nlMsg.text}</p>}
      <button type="button" onClick={() => nlMutation.mutate()} disabled={nlMutation.isPending}
        className="w-full bg-blue-600 text-white py-2 rounded-lg text-sm font-semibold hover:bg-blue-700 disabled:opacity-50 transition-colors">
        {nlMutation.isPending ? 'Saving…' : 'Save Newsletter Settings'}
      </button>
    </div>
  )
}
