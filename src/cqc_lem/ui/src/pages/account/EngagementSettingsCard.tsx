import { useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import api from '../../api/client'
import { useAuth } from '../../contexts/AuthContext'
import Toggle from '../../components/Toggle'
import { csv, parseCsv, CATCHUP_EVENTS } from './types'
import type { EngPrefs } from './types'
import { useRegisterSaveSection } from './SettingsSaveContext'
import { FIELD_LIMITS } from './fieldLimits'

// Voice & Tone and Engagement Targeting both edit the SAME engagement_preferences object and each
// PUT the full object. They must therefore share one piece of local state — otherwise saving one
// card would overwrite the other card's (stale) copy and revert its changes. So this single
// component owns the shared engPrefs state + mutation and renders both cards.
export default function EngagementSettingsCard() {
  const { sessionToken } = useAuth()
  const queryClient = useQueryClient()
  const [engPrefs, setEngPrefs] = useState<EngPrefs | null>(null)
  const [savedSig, setSavedSig] = useState<string | null>(null)
  const [engMsg, setEngMsg] = useState<{ ok: boolean; text: string } | null>(null)

  const { data: engData } = useQuery({
    queryKey: ['engagement-preferences', sessionToken],
    queryFn: () =>
      api
        .get(`/user/engagement-preferences?session_token=${encodeURIComponent(sessionToken!)}`)
        .then((r) => r.data.detail as EngPrefs),
    enabled: !!sessionToken,
    staleTime: 60 * 1000,
  })
  useEffect(() => {
    if (engData && !engPrefs) { setEngPrefs(engData); setSavedSig(JSON.stringify(engData)) }
  }, [engData])

  const setEng = (patch: Partial<EngPrefs>) => setEngPrefs((p) => (p ? { ...p, ...patch } : p))

  const engMutation = useMutation({
    mutationFn: () => api.put('/user/engagement-preferences', { session_token: sessionToken, ...engPrefs }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['engagement-preferences'] })
      setSavedSig(JSON.stringify(engPrefs))
      setEngMsg({ ok: true, text: 'Saved.' })
      setTimeout(() => setEngMsg(null), 3000)
    },
    onError: () => {
      setEngMsg({ ok: false, text: 'Could not save — try again.' })
      setTimeout(() => setEngMsg(null), 5000)
    },
  })

  const isDirty = !!engPrefs && savedSig !== null && JSON.stringify(engPrefs) !== savedSig
  useRegisterSaveSection('engagement', 'Voice, Focus & Targeting', isDirty,
    async () => { await engMutation.mutateAsync(); return true })

  if (!engPrefs) return null

  // Server-provided ceiling for the catch-up cap — 10/day on Professional/Enterprise, 5/day otherwise.
  const catchupCapMax = engPrefs.max_catchup_touches_allowed ?? 5

  // Server-provided defaults for the quality gates, shown as placeholders when the user hasn't
  // overridden them (issue #421).
  const gateDefaults = engPrefs.gate_defaults ?? { authenticity_score_min: 60, post_similarity_max_pct: 55 }

  // Every engagement section edits ONE shared object saved by ONE mutation, so a failure in any
  // field (e.g. an over-long value) fails the whole save. Show the result under whichever button
  // the user clicked — previously the message only rendered in the Targeting card, hiding errors
  // from anyone saving Voice & Tone or Focus & Goals.
  const saveBtn = (label: string) => (
    <div className="space-y-1.5">
      <button type="button" onClick={() => engMutation.mutate()} disabled={engMutation.isPending}
        className="w-full bg-blue-600 text-white py-2 rounded-lg text-sm font-semibold hover:bg-blue-700 disabled:opacity-50 transition-colors">
        {engMutation.isPending ? 'Saving…' : label}
      </button>
      {engMsg && (
        <p className={`text-sm font-medium ${engMsg.ok ? 'text-green-600' : 'text-red-600'}`}>{engMsg.text}</p>
      )}
    </div>
  )

  return (
    <>
      {/* Voice & Tone */}
      <div className="bg-white rounded-lg shadow-sm border border-gray-200 p-6 space-y-5">
        <h2 className="text-base font-semibold text-gray-700">Voice &amp; Tone</h2>
        <p className="text-xs text-gray-500">
          How AI comments and DMs should sound. Leave blank to infer purely from your profile.
        </p>
        <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
          <div>
            <label className="block text-sm font-medium text-gray-700 mb-1">Tone</label>
            <input type="text" value={engPrefs.tone || ''} onChange={(e) => setEng({ tone: e.target.value })}
              maxLength={FIELD_LIMITS.tone}
              placeholder="e.g. warm, authoritative"
              className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm" />
          </div>
          <div>
            <label className="block text-sm font-medium text-gray-700 mb-1">Comment length</label>
            <select value={engPrefs.comment_length} onChange={(e) => setEng({ comment_length: e.target.value })}
              className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm">
              <option value="short">Short (~30 words)</option>
              <option value="medium">Medium (~55 words · recommended)</option>
              <option value="long">Long (~90 words)</option>
            </select>
            <p className="text-xs text-gray-500 mt-1">
              Comments target substantive depth (≥15 words) — LinkedIn weights these far above one-liners. Medium is the default.
            </p>
          </div>
        </div>
        <div>
          <label className="block text-sm font-medium text-gray-700 mb-1">Style guidance</label>
          <input type="text" value={engPrefs.comment_style || ''} onChange={(e) => setEng({ comment_style: e.target.value })}
            maxLength={FIELD_LIMITS.comment_style}
            placeholder="e.g. ask a question, avoid buzzwords"
            className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm" />
        </div>
        <div className="flex items-center justify-between">
          <p className="text-sm font-medium text-gray-700">Use emojis</p>
          <Toggle on={engPrefs.use_emojis} onClick={() => setEng({ use_emojis: !engPrefs.use_emojis })} />
        </div>
        <div>
          <div className="flex items-center justify-between">
            <p className="text-sm font-medium text-gray-700">Use hashtags</p>
            <Toggle on={engPrefs.use_hashtags} onClick={() => setEng({ use_hashtags: !engPrefs.use_hashtags })} />
          </div>
          <p className="text-xs text-gray-500 mt-1">
            Off by default. In 2026 hashtags no longer expand reach: posts without them reach
            roughly 5–10% more people, so LEM writes without them. Turn this on only if you want a
            minimal set (at most 3) on the final line.
          </p>
        </div>
        {saveBtn('Save Voice & Tone')}
      </div>

      {/* Content Focus & Goals */}
      <div className="bg-white rounded-lg shadow-sm border border-gray-200 p-6 space-y-5">
        <h2 className="text-base font-semibold text-gray-700">Content Focus &amp; Goals</h2>
        <p className="text-xs text-gray-500">
          What your generated posts and comments should be about. These steer the ANGLE and keep
          content aligned to your business and personal goals — they never override the subject of the
          post being engaged with, and AI is instructed to never promote internal tools or software.
          Leave these blank and LEM defaults to relationship-building: every comment connects genuinely
          to the poster or to potential followers, grounded in the target post and your voice.
        </p>
        <div>
          <label className="block text-sm font-medium text-gray-700 mb-1">Focus topics</label>
          <input type="text" value={csv(engPrefs.focus_topics)}
            onChange={(e) => setEng({ focus_topics: parseCsv(e.target.value) })}
            placeholder="e.g. B2B sales, leadership, AI adoption"
            className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm" />
        </div>
        <div>
          <label className="block text-sm font-medium text-gray-700 mb-1">Business goals</label>
          <textarea value={engPrefs.business_goals || ''} rows={2} maxLength={FIELD_LIMITS.goals}
            onChange={(e) => setEng({ business_goals: e.target.value })}
            placeholder="e.g. Book 5 discovery calls/month with mid-market ops leaders"
            className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm" />
        </div>
        <div>
          <label className="block text-sm font-medium text-gray-700 mb-1">Personal goals</label>
          <textarea value={engPrefs.personal_goals || ''} rows={2} maxLength={FIELD_LIMITS.goals}
            onChange={(e) => setEng({ personal_goals: e.target.value })}
            placeholder="e.g. Build a reputation as a thoughtful voice in supply-chain tech"
            className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm" />
        </div>
        {/* Quality gates (issue #421) — the thresholds that decide whether a generated post is
            auto-scheduled or held for your review. Blank = keep LEM's default. */}
        <div className="border-t border-gray-100 pt-4 space-y-3">
          <h3 className="text-sm font-semibold text-gray-700">Review thresholds</h3>
          <p className="text-xs text-gray-500">
            A draft that fails one of these is held as PENDING with the reason shown in Content
            Studio, instead of being scheduled automatically. Leave blank to use the default.
          </p>
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
            <div>
              <label className="block text-sm font-medium text-gray-700 mb-1">
                Min. authenticity score
              </label>
              <input type="number" min={0} max={100}
                value={engPrefs.authenticity_score_min ?? ''}
                placeholder={`Default ${gateDefaults.authenticity_score_min}`}
                onChange={(e) => setEng({
                  authenticity_score_min: e.target.value === '' ? null : Number(e.target.value),
                })}
                className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm" />
              <p className="text-xs text-gray-400 mt-1">
                0-100 — how human/specific a draft must read before it can auto-schedule. Higher
                holds more posts for review; lower ships more of them unreviewed.
              </p>
            </div>
            <div>
              <label className="block text-sm font-medium text-gray-700 mb-1">
                Max. overlap with your recent posts
              </label>
              <input type="number" min={10} max={100}
                value={engPrefs.post_similarity_max_pct ?? ''}
                placeholder={`Default ${gateDefaults.post_similarity_max_pct}%`}
                onChange={(e) => setEng({
                  post_similarity_max_pct: e.target.value === '' ? null : Number(e.target.value),
                })}
                className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm" />
              <p className="text-xs text-gray-400 mt-1">
                10-100% word overlap against your own recent posts. Lower is stricter about
                near-duplicates.
              </p>
            </div>
          </div>
        </div>
        {saveBtn('Save Focus & Goals')}
      </div>

      {/* Engagement Targeting */}
      <div className="bg-white rounded-lg shadow-sm border border-gray-200 p-6 space-y-5">
        <h2 className="text-base font-semibold text-gray-700">Engagement Targeting</h2>
        <p className="text-xs text-gray-500">
          Control which posts LEM comments on. Comma-separated. Exclusions always win; if any
          "include" is set, a post must match one (keyword/author literal, or a topic via AI relevance).
        </p>
        {([
          ['include_topics', 'Include topics (AI relevance)'],
          ['exclude_topics', 'Exclude topics'],
          ['include_keywords', 'Include keywords'],
          ['exclude_keywords', 'Exclude keywords'],
          ['include_authors', 'Include authors'],
          ['exclude_authors', 'Exclude authors'],
        ] as [keyof EngPrefs, string][]).map(([field, label]) => (
          <div key={field}>
            <label className="block text-sm font-medium text-gray-700 mb-1">{label}</label>
            <input type="text" value={csv(engPrefs[field] as string[])}
              onChange={(e) => setEng({ [field]: parseCsv(e.target.value) } as Partial<EngPrefs>)}
              placeholder="comma, separated, values"
              className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm" />
          </div>
        ))}
        <div className="grid grid-cols-1 sm:grid-cols-3 gap-4">
          <div>
            <label className="block text-sm font-medium text-gray-700 mb-1">Min. reactions</label>
            <input type="number" min={0} value={engPrefs.min_reactions ?? ''}
              onChange={(e) => setEng({ min_reactions: e.target.value === '' ? null : Number(e.target.value) })}
              className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm" />
          </div>
          <div>
            <label className="block text-sm font-medium text-gray-700 mb-1">Max post age (hrs)</label>
            <input type="number" min={1} value={engPrefs.max_post_age_hours ?? ''}
              onChange={(e) => setEng({ max_post_age_hours: e.target.value === '' ? null : Number(e.target.value) })}
              placeholder="24"
              className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm" />
          </div>
          <div>
            <label className="block text-sm font-medium text-gray-700 mb-1">Max comments/day</label>
            <input type="number" min={0} value={engPrefs.max_comments_per_day}
              onChange={(e) => setEng({ max_comments_per_day: Number(e.target.value) })}
              className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm" />
          </div>
          <div>
            <label className="block text-sm font-medium text-gray-700 mb-1">Max DMs/day</label>
            <input type="number" min={0} value={engPrefs.max_dms_per_day}
              onChange={(e) => setEng({ max_dms_per_day: Number(e.target.value) })}
              className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm" />
          </div>
          <div>
            <label className="block text-sm font-medium text-gray-700 mb-1">Max invites/day</label>
            <input type="number" min={0} value={engPrefs.max_invites_per_day ?? 10}
              onChange={(e) => setEng({ max_invites_per_day: Number(e.target.value) })}
              className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm" />
            <p className="text-xs text-gray-400 mt-1">Combined daily cap across both proactive connect targets and reactive profile-viewer invites.</p>
          </div>
          <div>
            <label className="block text-sm font-medium text-gray-700 mb-1">Connection approval</label>
            <select value={engPrefs.connection_request_mode ?? 'auto_approve'}
              onChange={(e) => setEng({ connection_request_mode: e.target.value })}
              className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm">
              <option value="auto_approve">Auto-approve (queue targets immediately)</option>
              <option value="pre_review">Pre-review (approve each target first)</option>
            </select>
            <p className="text-xs text-gray-400 mt-1">Pre-review holds new connection targets in the Connections tab for your approval before LEM sends them.</p>
          </div>
          <div>
            <label className="block text-sm font-medium text-gray-700 mb-1">Connection targeting</label>
            <select value={engPrefs.connection_targeting_mode ?? 'suggest'}
              onChange={(e) => setEng({ connection_targeting_mode: e.target.value })}
              className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm">
              <option value="off">Off (no targets sourced)</option>
              <option value="suggest">Suggest (drafts for your approval)</option>
              <option value="auto_queue">Auto-queue (use the approval setting above)</option>
            </select>
            <p className="text-xs text-gray-400 mt-1">Finds ICP-fit people who engage with your content (and with the adjacent authors below) and drafts a personalized connect note. Suggest never sends without your approval.</p>
          </div>
          <div>
            <label className="block text-sm font-medium text-gray-700 mb-1">Min. ICP fit score</label>
            <input type="number" min={0} max={100} value={engPrefs.min_connection_icp_score ?? 55}
              onChange={(e) => setEng({ min_connection_icp_score: Number(e.target.value) })}
              className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm" />
            <p className="text-xs text-gray-400 mt-1">0-100 fit of their title/company/industry against your focus topics. Only applied to people whose profile LEM has scraped.</p>
          </div>
          <div className="sm:col-span-3">
            <label className="block text-sm font-medium text-gray-700 mb-1">Adjacent authors to source engagers from</label>
            <input type="text" value={csv(engPrefs.connection_target_authors)}
              onChange={(e) => setEng({ connection_target_authors: parseCsv(e.target.value) })}
              placeholder="https://www.linkedin.com/in/their-slug, https://www.linkedin.com/in/another"
              className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm" />
            <p className="text-xs text-gray-400 mt-1">Profile URLs of thought leaders or competitors in your space. LEM harvests the people commenting on their recent posts as warm 2nd-degree candidates. Leave blank to only target people who engage with your own posts.</p>
          </div>
          <div>
            <label className="block text-sm font-medium text-gray-700 mb-1">Max catch-up touches/day</label>
            <input type="number" min={0} max={catchupCapMax} value={engPrefs.max_catchup_touches_per_day ?? 5}
              onChange={(e) => setEng({ max_catchup_touches_per_day: Math.min(catchupCapMax, Number(e.target.value)) })}
              className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm" />
            <p className="text-xs text-gray-400 mt-1">
              Congratulations DMs sent per day from your LinkedIn Catch-up feed. They also count against Max DMs/day.
              {catchupCapMax > 5
                ? ' Your plan allows up to 10/day.'
                : ' Up to 5/day on your plan — 10/day is included with Professional and Enterprise.'}
            </p>
          </div>
          <div>
            <label className="block text-sm font-medium text-gray-700 mb-1">Catch-up approval</label>
            <select value={engPrefs.catchup_touch_mode ?? 'pre_review'}
              onChange={(e) => setEng({ catchup_touch_mode: e.target.value })}
              className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm">
              <option value="pre_review">Pre-review (edit + approve each message first)</option>
              <option value="auto_approve">Auto-use (send LinkedIn's draft as-is)</option>
            </select>
            <p className="text-xs text-gray-400 mt-1">Pre-review holds each congratulations in the Catch-up tab so you can edit it before it goes out; auto-use queues it straight into the capped drip.</p>
          </div>
          <div>
            <label className="block text-sm font-medium text-gray-700 mb-1">Catch-up message</label>
            <select value={engPrefs.catchup_message_source ?? 'linkedin'}
              onChange={(e) => setEng({ catchup_message_source: e.target.value })}
              className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm">
              <option value="linkedin">LinkedIn's suggested response (default)</option>
              <option value="ai">Customize with AI (your DM template + voice)</option>
            </select>
            <p className="text-xs text-gray-400 mt-1">LinkedIn already drafts a congratulations for each moment — we use that as the baseline, no AI required. Switch to AI only if you want the message rewritten in your voice from your DM template.</p>
          </div>
          <div>
            <label className="block text-sm font-medium text-gray-700 mb-1">Auto-generated video quality</label>
            <select value={engPrefs.default_video_quality ?? 'standard'}
              onChange={(e) => setEng({ default_video_quality: e.target.value })}
              className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm">
              <option value="standard">Standard (free)</option>
              <option value="premium">Premium (uses 1 video credit)</option>
              <option value="premium_top">Premium Top (uses 3 video credits)</option>
            </select>
            <p className="text-xs text-gray-400 mt-1">Premium spends video credits per auto-generated video; falls back to standard when you have none.</p>
          </div>
        </div>
        <div className="border-t border-gray-100 pt-4 space-y-2">
          <div className="flex items-center justify-between">
            <div className="pr-4">
              <p className="text-sm font-medium text-gray-700">Comment anyway if my filters match nothing</p>
              <p className="text-xs text-gray-400">If none of the posts in your feed match your include topics/keywords, comment on the best feed posts anyway (excludes, recency and min-reactions still apply). LinkedIn already curates your feed to relevant content.</p>
            </div>
            <Toggle on={engPrefs.feed_fallback_when_empty} onClick={() => setEng({ feed_fallback_when_empty: !engPrefs.feed_fallback_when_empty })} />
          </div>
          {engPrefs.feed_reach && (
            <div className="rounded-lg bg-gray-50 border border-gray-100 p-3 text-xs text-gray-600">
              <p className="font-medium text-gray-700 mb-1">Last feed scan reach</p>
              <p>
                Examined <span className="font-semibold">{engPrefs.feed_reach.examined}</span> posts →{' '}
                <span className="font-semibold">{engPrefs.feed_reach.passed_filters}</span> passed your recency / min-reactions filters →{' '}
                <span className="font-semibold">{engPrefs.feed_reach.matched_topics}</span> matched your topics/keywords → commented on{' '}
                <span className="font-semibold">{engPrefs.feed_reach.commented}</span>
                {engPrefs.feed_reach.fallback_used ? ' (used fallback)' : ''}.
              </p>
              {engPrefs.feed_reach.examined > 0 && engPrefs.feed_reach.matched_topics === 0 && (
                <p className="mt-1 text-amber-600">Your include filters matched nothing in the last scan — consider loosening topics/keywords, lowering min-reactions, or widening max post age.</p>
              )}
            </div>
          )}
        </div>
        <div className="flex items-center justify-between">
          <div className="pr-4">
            <p className="text-sm font-medium text-gray-700">Put links in the first comment</p>
            <p className="text-xs text-gray-400">When a post contains an external link, publish the post without it and drop the link into the first comment instead. Links in the body cost roughly 60-68% of a post's reach.</p>
          </div>
          <Toggle on={engPrefs.link_in_first_comment} onClick={() => setEng({ link_in_first_comment: !engPrefs.link_in_first_comment })} />
        </div>
        <div className="border-t border-gray-100 pt-4">
          <p className="text-sm font-medium text-gray-700">Catch-up milestones to congratulate</p>
          <p className="text-xs text-gray-400 mb-2">
            Which network moments LEM drafts a congratulations for — all six are yours to turn on.
            New job and promotion are on by default because they're real reasons to reconnect; the
            rest are opt-in. Unchecking every type turns Catch-up off.
          </p>
          <div className="flex flex-wrap gap-3">
            {CATCHUP_EVENTS.map((ev) => {
              const selected = (engPrefs.catchup_event_types ?? []).includes(ev.key)
              return (
                <label key={ev.key} className="flex items-center gap-1.5 text-sm text-gray-600">
                  <input type="checkbox" checked={selected}
                    onChange={() => setEng({
                      catchup_event_types: selected
                        ? (engPrefs.catchup_event_types ?? []).filter((k) => k !== ev.key)
                        : [...(engPrefs.catchup_event_types ?? []), ev.key],
                    })} />
                  {ev.label}
                </label>
              )
            })}
          </div>
        </div>
        <div className="flex items-center justify-between">
          <p className="text-sm font-medium text-gray-700">Reply to comments on my posts</p>
          <Toggle on={engPrefs.reply_to_own_comments} onClick={() => setEng({ reply_to_own_comments: !engPrefs.reply_to_own_comments })} />
        </div>
        <div className="border-t border-gray-100 pt-4">
          <label className="block text-sm font-medium text-gray-700 mb-1">Reply follow-up mode</label>
          <select value={engPrefs.reply_check_mode ?? 'event'}
            onChange={(e) => setEng({ reply_check_mode: e.target.value })}
            className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm">
            <option value="event">Event-driven — reply when notified (recommended)</option>
            <option value="scheduled">Scheduled — check on a timer</option>
            <option value="off">Off — don't auto-reply</option>
          </select>
          {engPrefs.reply_check_mode === 'event' && (
            <div className="mt-2 text-xs text-gray-500 space-y-3">
              <p className="text-gray-600">
                We reply to comments on your posts the moment LinkedIn emails you about them — no browser
                polling, so it won't trip LinkedIn's rate limits. One-time setup (about 2 minutes):
              </p>

              <div>
                <p className="font-medium text-gray-700">1. Turn on LinkedIn email notifications for comments</p>
                <p>
                  On LinkedIn: <span className="text-gray-600">Me → Settings &amp; Privacy → Notifications →
                  “Posts, comments and mentions”</span>, and make sure <span className="font-medium">Email</span> is
                  ON for comments &amp; replies on your posts. (Without this, LinkedIn never sends the email we listen for.)
                </p>
              </div>

              <div>
                <p className="font-medium text-gray-700">2. Copy your personal forwarding address</p>
                {engPrefs.reply_inbound_address ? (
                  <div className="flex items-center gap-2 mt-1">
                    <code className="bg-gray-100 rounded px-2 py-1 text-gray-700 break-all">{engPrefs.reply_inbound_address}</code>
                    <button type="button" onClick={() => navigator.clipboard?.writeText(engPrefs.reply_inbound_address || '')}
                      className="text-blue-600 hover:underline shrink-0">Copy</button>
                  </div>
                ) : (
                  <p className="text-gray-400">Save your settings once to generate your address.</p>
                )}
                <p className="mt-1 text-gray-400">Keep this private — it's unique to your account.</p>
              </div>

              <div>
                <p className="font-medium text-gray-700">3. Auto-forward those emails to it (Gmail)</p>
                <p>
                  In Gmail: <span className="text-gray-600">Settings → Forwarding and POP/IMAP → Add a forwarding
                  address</span> (paste the address above and confirm it). Then <span className="text-gray-600">Settings →
                  Filters and Blocked Addresses → Create a new filter</span> with
                  From <code className="bg-gray-100 rounded px-1">linkedin.com</code> and
                  Subject <code className="bg-gray-100 rounded px-1">commented OR replied</code>, click
                  <span className="italic"> Create filter</span>, then check
                  <span className="italic"> “Forward it to”</span> and pick your address. Using another provider?
                  Any rule that forwards LinkedIn comment emails to the address works.
                </p>
                <p className="mt-1 text-green-700">
                  Good news: when Gmail sends the "verify permission" confirmation to your address, we
                  confirm it for you automatically — no need to fish out the code.
                </p>
                {engPrefs.gmail_forward_confirmation && (
                  <div className="mt-1">
                    {engPrefs.gmail_forward_confirmation.confirmed ? (
                      <p className="text-green-700">✅ Gmail forwarding was auto-confirmed.</p>
                    ) : engPrefs.gmail_forward_confirmation.code ? (
                      <p className="text-amber-600">
                        We received Gmail's confirmation but couldn't auto-click it. If forwarding still shows
                        "pending" in Gmail, enter this code there:{' '}
                        <code className="bg-gray-100 rounded px-1">{engPrefs.gmail_forward_confirmation.code}</code>
                      </p>
                    ) : null}
                  </div>
                )}
              </div>

              <p className="text-gray-400">
                That's it — new comments on your posts get an on-topic reply within minutes.
              </p>
            </div>
          )}
          {engPrefs.reply_check_mode === 'scheduled' && (
            <div className="mt-2 space-y-2">
              <div>
                <label className="block text-xs font-medium text-gray-600 mb-1">Reply checks per day (2–12)</label>
                <input type="number" min={2} max={12} value={engPrefs.reply_sweeps_per_day ?? 2}
                  onChange={(e) => setEng({ reply_sweeps_per_day: Math.min(12, Math.max(2, Number(e.target.value) || 2)) })}
                  className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm" />
              </div>
              <p className="text-xs text-amber-600">⚠ Scheduled checks open a browser session each run, which can get your account temporarily rate-limited (HTTP 429) by LinkedIn. Event-driven is recommended.</p>
            </div>
          )}
        </div>
        {saveBtn('Save Targeting')}
      </div>
    </>
  )
}
