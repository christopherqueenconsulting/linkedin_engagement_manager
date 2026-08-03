import { useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import api from '../../api/client'
import { useAuth } from '../../contexts/AuthContext'
import { ROSTER_BLOCKED_BADGE_STREAK, TARGET_CATEGORIES } from './types'
import type { EngagementTarget, EngagementTargetCategory } from './types'
import { useRegisterSaveSection, sectionSaveCallbacks } from './SettingsSaveContext'
import { useEngagementPrefs } from './settings/EngagementPrefsContext'
import { ConflictNotices } from './settings/Field'

type RosterResponse = { targets: EngagementTarget[]; suggestions: EngagementTarget[] }

// Every row's number field says only "/wk" next to it, which told a sighted user nothing about what
// it controls (issue #956) — so the column header, the tooltip and the aria-label all say it here.
const PER_WEEK_LABEL = 'Max comments/wk'
const PER_WEEK_HINT =
  "LEM will comment on this account's recent posts at most this many times per week (0 pauses the account)."

// What LEM learned about a target it could not act on (issue #962). A blocked target is not an
// error — the author simply restricts who may comment — so the badge says what to DO about it.
// Following and being ABLE to comment are not the same thing: a connections-only commenter stays
// blocked after a follow, which is why the copy names both moves.
const BLOCKED_LABEL = "Can't comment — follow or connect with this account"
const BLOCKED_HINT =
  'This account\'s recent posts rendered with no comment box, which is how LinkedIn shows a post ' +
  'that only accepts comments from connections or followers. Following may unlock it; if the ' +
  'author is connections-only, you have to connect.'
const FOLLOW_FAILED_LABEL = 'Follow failed'
const FOLLOW_FAILED_HINT =
  'LEM found a Follow button but it never flipped to Following, twice. Follow this account by ' +
  'hand — LEM will not keep retrying.'
const FOLLOWING_LABEL = 'Following'

function TargetBadges({ target }: { target: EngagementTarget }) {
  const blocked = (target.comment_blocked_streak ?? 0) >= ROSTER_BLOCKED_BADGE_STREAK
  const failed = target.follow_status === 'follow_failed'
  const following = target.follow_status === 'following'
  if (!blocked && !failed && !following) return null
  return (
    <div className="sm:col-span-12 flex flex-wrap gap-2 text-xs -mt-1"
         data-testid={`roster-badges-${target.id ?? target.profile_url}`}>
      {blocked && (
        <span title={BLOCKED_HINT} data-testid="roster-blocked-badge"
              className="bg-amber-100 text-amber-900 rounded-full px-2 py-0.5 font-medium">
          {BLOCKED_LABEL}
        </span>
      )}
      {failed && (
        <span title={FOLLOW_FAILED_HINT} data-testid="roster-follow-failed-badge"
              className="bg-red-100 text-red-800 rounded-full px-2 py-0.5 font-medium">
          {FOLLOW_FAILED_LABEL}
        </span>
      )}
      {following && !failed && (
        <span data-testid="roster-following-badge"
              className="bg-gray-100 text-gray-600 rounded-full px-2 py-0.5">
          {FOLLOWING_LABEL}
        </span>
      )}
    </div>
  )
}

const blank = (): EngagementTarget => ({
  profile_url: '', name: '', category: 'peer', max_comments_per_week: 2, active: true, source: 'user',
})

// The blend the rotation aims for — shown live so the operator can see how far their roster is from
// the 50/30/20 mix the commenting task draws on.
function MixBar({ targets }: { targets: EngagementTarget[] }) {
  const active = targets.filter((t) => t.active && t.profile_url.trim())
  if (active.length === 0) return null
  return (
    <div className="flex flex-wrap gap-3 text-xs text-gray-600" data-testid="roster-mix">
      {TARGET_CATEGORIES.map((c) => {
        const n = active.filter((t) => t.category === c.key).length
        return (
          <span key={c.key}>
            {c.label}: <span className="font-semibold">{n}</span> (
            {Math.round((n / active.length) * 100)}%)
          </span>
        )
      })}
    </div>
  )
}

/**
 * Target-creator engagement roster (issue #616). LEM comments on these accounts' recent posts
 * BEFORE it looks at the home feed, at most `max_comments_per_week` times each so the rotation
 * never reads like a pod.
 */
export default function EngagementRosterCard() {
  const { sessionToken } = useAuth()
  const queryClient = useQueryClient()
  // The auto-follow toggle is an engagement PREFERENCE, so it shares the one prefs row and the one
  // mutation every settings section writes through — a second writer here would save the other
  // sections' stale copies over the user's edits (see EngagementPrefsContext).
  const { eng, setEng, save: savePrefs } = useEngagementPrefs()
  const [targets, setTargets] = useState<EngagementTarget[]>([])
  const [savedSig, setSavedSig] = useState<string | null>(null)
  const [init, setInit] = useState(false)
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null)

  const { data } = useQuery({
    queryKey: ['engagement-targets', sessionToken],
    queryFn: () =>
      api
        .get(`/user/engagement-targets?session_token=${encodeURIComponent(sessionToken!)}`)
        .then((r) => r.data.detail as RosterResponse),
    enabled: !!sessionToken,
    staleTime: 60 * 1000,
  })

  useEffect(() => {
    if (data && !init) {
      setTargets(data.targets)
      setSavedSig(JSON.stringify(data.targets))
      setInit(true)
    }
  }, [data, init])

  const update = (idx: number, patch: Partial<EngagementTarget>) =>
    setTargets((ts) => ts.map((t, i) => (i === idx ? { ...t, ...patch } : t)))
  const addRow = () => setTargets((ts) => [...ts, blank()])
  const addSuggestion = (s: EngagementTarget) =>
    setTargets((ts) => (ts.some((t) => t.profile_url === s.profile_url) ? ts : [...ts, { ...s }]))

  const removeMutation = useMutation({
    mutationFn: (profile_url: string) =>
      api.delete('/user/engagement-targets', { data: { session_token: sessionToken, profile_url } }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['engagement-targets'] }),
  })
  const removeRow = (idx: number) => {
    const target = targets[idx]
    setTargets((ts) => ts.filter((_, i) => i !== idx))
    // Only a row that was actually saved needs a server-side delete; a never-saved draft row just
    // disappears from local state.
    if (target?.id) removeMutation.mutate(target.profile_url)
  }

  const saveMutation = useMutation({
    mutationFn: () =>
      api.put('/user/engagement-targets', {
        session_token: sessionToken,
        targets: targets.filter((t) => t.profile_url.trim()),
      }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['engagement-targets'] })
      setSavedSig(JSON.stringify(targets))
      setMsg({ ok: true, text: 'Engagement roster saved.' })
      setTimeout(() => setMsg(null), 3000)
    },
    onError: () => {
      setMsg({ ok: false, text: 'Could not save — try again.' })
      setTimeout(() => setMsg(null), 5000)
    },
  })

  // The toggle lives on this card but is stored in the shared prefs row, so "Save Roster" has to
  // write both — a switch that silently needed a second save button elsewhere is how a user ends up
  // believing auto-follow is on when it never was. Tracked from the control itself rather than by
  // diffing loaded state, so the prefs row is only rewritten when the user actually moved it.
  const [autoFollowTouched, setAutoFollowTouched] = useState(false)
  const autoFollow = !!eng?.roster_auto_follow

  const saveRoster = async (): Promise<boolean> => {
    const analytics = sectionSaveCallbacks('engagement-roster')
    try {
      await saveMutation.mutateAsync()
    } catch {
      analytics.onError()
      return false
    }
    analytics.onSuccess()
    if (autoFollowTouched) {
      if (!(await savePrefs())) return false
      setAutoFollowTouched(false)
    }
    return true
  }

  const isDirty = init && savedSig !== null && JSON.stringify(targets) !== savedSig
  useRegisterSaveSection('engagement-roster', 'Engagement Roster', isDirty, saveRoster)

  if (!init) return null

  const suggestions = (data?.suggestions ?? []).filter(
    (s) => !targets.some((t) => t.profile_url === s.profile_url))

  return (
    <div className="bg-white rounded-lg shadow-sm border border-gray-200 p-6 space-y-4"
         data-testid="engagement-roster">
      <h2 className="text-base font-semibold text-gray-700">Engagement Roster</h2>
      <p className="text-xs text-gray-500">
        LEM comments on these accounts' recent posts <span className="font-semibold">before</span> it
        looks at your home feed. The <span className="font-semibold">{PER_WEEK_LABEL}</span> number on
        each row is that account's weekly ceiling — set it to 0 to pause an account without removing
        it. Aim for 50–100 accounts, roughly 50% peers / 30% ICP / 20% large creators. Off-topic posts
        are always skipped, roster or not.
      </p>
      {/* The account-level half of the blocked-roster signal (issue #962): the per-row badges say
          WHICH accounts, this says the roster as a whole has stopped working. Tolerant of no
          provider, so the card still renders standalone. */}
      <ConflictNotices anchor="engagement_roster" />
      <MixBar targets={targets} />

      <div className="space-y-2">
        {/* Column headers name each field on wide screens; on mobile the rows stack, so every field
            carries its own inline label instead. */}
        {targets.length > 0 && (
          <div className="hidden sm:grid sm:grid-cols-12 gap-2 items-end text-xs font-medium text-gray-500"
               data-testid="roster-headers">
            <span className="sm:col-span-4">Profile URL</span>
            <span className="sm:col-span-3">Name</span>
            <span className="sm:col-span-2">Type</span>
            <span className="sm:col-span-2" title={PER_WEEK_HINT}>{PER_WEEK_LABEL}</span>
            <span className="sm:col-span-1 text-right">On</span>
          </div>
        )}
        {targets.map((t, idx) => (
          <div key={t.id ?? `new-${idx}`} className="grid grid-cols-1 sm:grid-cols-12 gap-2 items-center">
            <input type="text" value={t.profile_url} maxLength={512}
              onChange={(e) => update(idx, { profile_url: e.target.value })}
              placeholder="https://www.linkedin.com/in/their-handle"
              aria-label="Profile URL"
              className="sm:col-span-4 border border-gray-300 rounded-lg px-3 py-2 text-sm" />
            <input type="text" value={t.name ?? ''} maxLength={255}
              onChange={(e) => update(idx, { name: e.target.value })}
              placeholder="Name (optional)" aria-label="Name"
              className="sm:col-span-3 border border-gray-300 rounded-lg px-3 py-2 text-sm" />
            <select value={t.category} aria-label="Category"
              onChange={(e) => update(idx, { category: e.target.value as EngagementTargetCategory })}
              className="sm:col-span-2 border border-gray-300 rounded-lg px-2 py-2 text-sm">
              {TARGET_CATEGORIES.map((c) => (
                <option key={c.key} value={c.key} title={c.hint}>{c.label}</option>
              ))}
            </select>
            <label className="sm:col-span-2 flex items-center gap-1 text-xs text-gray-500"
                   title={PER_WEEK_HINT}>
              <input type="number" min={0} max={14} value={t.max_comments_per_week}
                onChange={(e) => update(idx, { max_comments_per_week: Number(e.target.value) })}
                aria-label={PER_WEEK_LABEL} title={PER_WEEK_HINT}
                className="w-14 border border-gray-300 rounded px-1 py-1" />
              <span className="sm:hidden">{PER_WEEK_LABEL}</span>
              <span className="hidden sm:inline">/wk</span>
            </label>
            <div className="sm:col-span-1 flex items-center justify-end gap-2 text-xs">
              <label className="flex items-center gap-1 text-gray-500">
                <input type="checkbox" checked={t.active} aria-label="Active"
                  onChange={(e) => update(idx, { active: e.target.checked })} />
                On
              </label>
              <button type="button" onClick={() => removeRow(idx)}
                aria-label={`Remove ${t.name || t.profile_url || 'account'}`}
                title="Remove account"
                className="text-red-500 hover:text-red-600">×</button>
            </div>
            <TargetBadges target={t} />
          </div>
        ))}
      </div>

      <button type="button" onClick={addRow}
        className="text-xs text-blue-600 font-medium hover:text-blue-700">+ Add account</button>

      {eng && (
        <div className="border-t border-gray-100 pt-3">
          <label className="flex items-start gap-2 text-sm text-gray-700">
            <input type="checkbox" checked={autoFollow} data-testid="roster-auto-follow"
              aria-label="Follow blocked roster accounts automatically"
              onChange={(e) => {
                setEng({ roster_auto_follow: e.target.checked })
                setAutoFollowTouched(true)
              }}
              className="mt-0.5" />
            <span>
              Follow roster accounts automatically
              <span className="block text-xs text-gray-500">
                Off by default. When on, LEM follows up to{' '}
                <span className="font-semibold">{eng.max_follows_per_day ?? 3}</span> roster accounts
                a day while it is already on their activity page — never in a separate browsing
                session. Following can unlock a followers-only account; a connections-only one still
                needs a connection request. Change the daily number under Volume → Daily caps.
              </span>
            </span>
          </label>
        </div>
      )}

      {suggestions.length > 0 && (
        <div className="border-t border-gray-100 pt-3 space-y-2">
          <p className="text-xs text-gray-500">
            Suggested seeds — people who recently engaged with your posts. Add them, then set the
            right category:
          </p>
          <div className="flex flex-wrap gap-2">
            {suggestions.map((s) => (
              <button key={s.profile_url} type="button" onClick={() => addSuggestion(s)}
                className="text-xs bg-gray-100 hover:bg-gray-200 text-gray-700 rounded-full px-3 py-1">
                + {s.name || s.profile_url}
              </button>
            ))}
          </div>
        </div>
      )}

      {msg && <p className={`text-sm font-medium ${msg.ok ? 'text-green-600' : 'text-red-600'}`}>{msg.text}</p>}
      <button type="button" onClick={() => { void saveRoster() }}
        disabled={saveMutation.isPending}
        className="w-full bg-blue-600 text-white py-2 rounded-lg text-sm font-semibold hover:bg-blue-700 disabled:opacity-50 transition-colors">
        {saveMutation.isPending ? 'Saving…' : 'Save Roster'}
      </button>
    </div>
  )
}
