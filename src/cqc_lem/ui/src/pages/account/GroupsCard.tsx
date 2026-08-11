import { useEffect, useState } from 'react'
import { useMutation, useQuery } from '@tanstack/react-query'
import api from '../../api/client'
import { useAuth } from '../../contexts/AuthContext'
import Toggle from '../../components/Toggle'
import { maskProps } from '../../utils/analytics'
import type { GroupPostDraft, UserGroup } from './types'
import { useRegisterSaveSection, sectionSaveCallbacks } from './SettingsSaveContext'

const groupLabel = (g: UserGroup) => g.group_name || `Group ${g.group_id}`
// LinkedIn's own post cap, mirrored from the API's _LEN_GROUP_POST so the counter, the Save button
// and the server's 422 all gate on the same number.
const GROUP_POST_MAX = 3000
// A group synced before #769 has no post_enabled in an older cached payload — treat a missing flag
// as ON, which is what the column defaults to server-side.
const normalize = (rows: UserGroup[]) => rows.map((g) => ({ ...g, post_enabled: g.post_enabled !== false }))
// WHICH groups take posts is the only input the server's rotation reads, so an unsaved change to it
// invalidates the marked next group.
const postSig = (rows: UserGroup[]) =>
  rows.filter((g) => g.post_enabled).map((g) => g.group_id).sort().join('|')

export default function GroupsCard() {
  const { sessionToken } = useAuth()
  const [groups, setGroups] = useState<UserGroup[]>([])
  const [groupsInit, setGroupsInit] = useState(false)
  const [savedSig, setSavedSig] = useState<string | null>(null)
  const [savedPostSig, setSavedPostSig] = useState('')
  const [groupsMsg, setGroupsMsg] = useState<{ ok: boolean; text: string } | null>(null)
  const [draftText, setDraftText] = useState<string | null>(null)
  const [draftMsg, setDraftMsg] = useState<{ ok: boolean; text: string } | null>(null)

  const { data: groupsData, refetch: refetchGroups } = useQuery({
    queryKey: ['user-groups', sessionToken],
    queryFn: () =>
      api
        .get(`/user/groups?session_token=${encodeURIComponent(sessionToken!)}`)
        .then((r) => r.data.detail as UserGroup[]),
    enabled: !!sessionToken,
    staleTime: 60 * 1000,
  })
  useEffect(() => {
    if (groupsData && !groupsInit) {
      const normalized = normalize(groupsData)
      setGroups(normalized)
      setSavedSig(JSON.stringify(normalized))
      setSavedPostSig(postSig(normalized))
      setGroupsInit(true)
    }
  }, [groupsData, groupsInit])

  // The post queued for the next weekly group slot. It is written days ahead precisely so it can be
  // read and revised here before it ships (issue #932).
  const { data: draft, refetch: refetchDraft } = useQuery({
    queryKey: ['group-post-draft', sessionToken],
    queryFn: () =>
      api
        .get(`/user/group-post-draft?session_token=${encodeURIComponent(sessionToken!)}`)
        .then((r) => (r.data.detail as GroupPostDraft | null) ?? null),
    enabled: !!sessionToken,
    staleTime: 60 * 1000,
  })
  // Adopt the server's text only until the user starts typing — a background refetch must not
  // discard an edit in progress.
  useEffect(() => {
    setDraftText((cur) => (cur === null ? draft?.content ?? null : cur))
  }, [draft])

  const draftMutation = useMutation({
    mutationFn: (body: { content?: string; status?: string }) =>
      api.put('/user/group-post-draft', { session_token: sessionToken, ...body }),
    onSuccess: async (_res, body) => {
      // Only a skip drops the local text — re-adopting the saved copy would clobber anything the
      // user typed while the save was in flight.
      if (body.status) setDraftText(null)
      await refetchDraft()
      setDraftMsg({
        ok: true,
        text: body.status === 'skipped' ? 'Skipped — no group post this week.'
          : body.status === 'ready' ? 'Back in the queue for this week.'
          : 'Saved.',
      })
      setTimeout(() => setDraftMsg(null), 3000)
    },
    onError: () => {
      setDraftMsg({ ok: false, text: 'Could not save — try again.' })
      setTimeout(() => setDraftMsg(null), 5000)
    },
  })

  const toggleGroup = (gid: string, field: 'enabled' | 'post_enabled') =>
    setGroups((gs) => gs.map((g) => (g.group_id === gid ? { ...g, [field]: !g[field] } : g)))

  const groupsMutation = useMutation({
    mutationFn: () =>
      api.put('/user/groups', {
        session_token: sessionToken,
        groups: Object.fromEntries(
          groups.map((g) => [g.group_id, { enabled: g.enabled, post_enabled: !!g.post_enabled }])
        ),
      }),
    onSuccess: async () => {
      // Saving the flags moves the rotation — a group whose Post switch just went on and has never
      // been posted in is now next. Adopt the answer the server re-resolves instead of leaving the
      // card naming the group it was mounted with.
      const fresh = await refetchGroups()
      const normalized = normalize(fresh.data ?? groups)
      setGroups(normalized)
      setSavedSig(JSON.stringify(normalized))
      setSavedPostSig(postSig(normalized))
      setGroupsMsg({ ok: true, text: 'Saved.' })
      setTimeout(() => setGroupsMsg(null), 3000)
    },
    onError: () => {
      setGroupsMsg({ ok: false, text: 'Could not save — try again.' })
      setTimeout(() => setGroupsMsg(null), 5000)
    },
  })

  const isDirty = groupsInit && savedSig !== null && JSON.stringify(groups) !== savedSig
  useRegisterSaveSection('groups', 'LinkedIn Groups', isDirty,
    async () => { await groupsMutation.mutateAsync(); return true })

  // A rewrite of the queued post is a settings change like any other on this page: without its own
  // registration, Save All would report success having saved only the toggles, and the unsaved-guard
  // would let the user walk away — and the text they thought they'd replaced would publish.
  const draftDirty = !!draft && draftText !== null && draftText !== draft.content &&
    !!draftText.trim() && draftText.length <= GROUP_POST_MAX
  // A skipped draft is still shown here since #1224 — it is restorable until the slot passes — so
  // this card must not describe it as "what LEM will publish".
  const draftSkipped = draft?.status === 'skipped'
  useRegisterSaveSection('group-post', 'Next group post', draftDirty,
    async () => { await draftMutation.mutateAsync({ content: draftText as string }); return true })

  if (!(groupsInit && groups.length > 0)) return null

  // The server decides the rotation (least-recently-posted first) and marks the row. Any unsaved
  // change to which groups take posts changes that answer — including switching a never-posted
  // group ON, which jumps it to the front — so the card stops naming a group until the save
  // re-resolves it rather than confidently naming the wrong one.
  const postSelectionDirty = postSig(groups) !== savedPostSig
  const markedNext = postSelectionDirty ? undefined : groups.find((g) => g.is_next_post && g.post_enabled)
  const postingGroups = groups.filter((g) => g.post_enabled)

  return (
    <div className="bg-white rounded-lg shadow-sm border border-gray-200 p-6 space-y-4">
      <h2 className="text-base font-semibold text-gray-700">LinkedIn Groups</h2>
      <div className="text-xs text-gray-500 space-y-1">
        <p>Two separate things happen in your joined groups, and each has its own switch per group. Both are on by default.</p>
        <p>
          <span className="font-semibold text-gray-700">Comment</span> — LEM leaves value-add comments on
          other members' posts in that group, daily, out of your normal daily comment budget.
        </p>
        <p>
          <span className="font-semibold text-gray-700">Post</span> — about once a week LEM publishes{' '}
          <span className="font-semibold text-gray-700">one original post into one group</span>, written fresh
          for that group's members. Your scheduled feed posts are never duplicated, reshared or cross-posted
          into groups, and the same post never goes to more than one group.
        </p>
        <p>
          The weekly slot rotates: it goes to whichever group with <span className="font-semibold text-gray-700">Post</span>{' '}
          on has waited longest for its turn. A group that turns out not to take member posts
          (announcement or admin-only) uses up its turn and goes to the back of the queue, so it
          never holds up the others.
        </p>
        <p>
          That post is written a couple of days early and waits below, so you can read it — and
          rewrite as much of it as you like — before it goes out.
        </p>
      </div>

      {draft && draftText !== null && (
        <div className="border border-blue-200 bg-blue-50/40 rounded p-3 space-y-2">
          <h3 className="text-sm font-semibold text-gray-700">
            Next group post — {draft.group_name || `Group ${draft.group_id}`}
          </h3>
          <p className="text-xs text-gray-600">
            {draftSkipped
              ? 'Skipped — nothing goes out this week unless you put it back in the queue. '
                + 'Images and video for a group post live in the Content Studio.'
              : 'This exact text is what LEM will publish at the next weekly group slot. Edit it, '
                + 'or skip it and no group post goes out this week.'}
          </p>
          <textarea
            aria-label="Group post text"
            value={draftText}
            onChange={(e) => setDraftText(e.target.value)}
            rows={8}
            {...maskProps('w-full border border-gray-300 rounded p-2 text-sm text-gray-800')}
          />
          <div className="flex items-center justify-between gap-3">
            <span className={`text-xs ${draftText.length > GROUP_POST_MAX ? 'text-red-600' : 'text-gray-500'}`}>
              {draftText.length}/{GROUP_POST_MAX}
            </span>
            <span className="flex items-center gap-2">
              <button type="button"
                onClick={() => draftMutation.mutate({ status: draftSkipped ? 'ready' : 'skipped' })}
                disabled={draftMutation.isPending}
                className="px-3 py-1.5 rounded-lg text-sm font-semibold border border-gray-300 text-gray-700 hover:bg-gray-100 disabled:opacity-50 transition-colors">
                {draftSkipped ? 'Put back in the queue' : 'Skip this week'}
              </button>
              <button type="button"
                onClick={() => draftMutation.mutate({ content: draftText }, sectionSaveCallbacks('group-post'))}
                disabled={draftMutation.isPending || !draftDirty}
                className="px-3 py-1.5 rounded-lg text-sm font-semibold bg-blue-600 text-white hover:bg-blue-700 disabled:opacity-50 transition-colors">
                {draftMutation.isPending ? 'Saving…' : 'Save post'}
              </button>
            </span>
          </div>
        </div>
      )}
      {/* Outside the panel: a skip removes the panel, and the confirmation has to outlive it. */}
      {draftMsg && (
        <p className={`text-sm font-medium ${draftMsg.ok ? 'text-green-600' : 'text-red-600'}`}>{draftMsg.text}</p>
      )}

      <p className="text-xs text-gray-600 bg-gray-50 border border-gray-200 rounded p-2">
        {postingGroups.length === 0
          ? 'Next group post: none — no group has Post turned on, so LEM will not post in any group.'
          : markedNext
            ? `Next group post: ${groupLabel(markedNext)}.`
            : 'Next group post: picked from the groups below when you save.'}
      </p>

      <div className="divide-y divide-gray-100">
        <div className="flex items-center justify-between pb-2 text-[11px] font-semibold uppercase tracking-wide text-gray-400">
          <span>Group</span>
          <span className="flex items-center gap-3">
            <span className="w-11 text-center">Comment</span>
            <span className="w-11 text-center">Post</span>
          </span>
        </div>
        {groups.map((g) => (
          <div key={g.group_id} className="flex items-center justify-between py-2">
            <span className="text-sm text-gray-700 truncate pr-3">
              {groupLabel(g)}
              {markedNext?.group_id === g.group_id && (
                <span className="ml-2 text-[10px] font-semibold uppercase text-blue-700 bg-blue-50 rounded px-1.5 py-0.5">
                  Next post
                </span>
              )}
            </span>
            <span className="flex items-center gap-3">
              <Toggle on={g.enabled} onClick={() => toggleGroup(g.group_id, 'enabled')}
                ariaLabel={`Comment in ${groupLabel(g)}`} />
              <Toggle on={!!g.post_enabled} onClick={() => toggleGroup(g.group_id, 'post_enabled')}
                ariaLabel={`Post in ${groupLabel(g)}`} />
            </span>
          </div>
        ))}
      </div>
      {groupsMsg && <p className={`text-sm font-medium ${groupsMsg.ok ? 'text-green-600' : 'text-red-600'}`}>{groupsMsg.text}</p>}
      <button type="button" onClick={() => groupsMutation.mutate(undefined, sectionSaveCallbacks('groups'))}
        disabled={groupsMutation.isPending}
        className="w-full bg-blue-600 text-white py-2 rounded-lg text-sm font-semibold hover:bg-blue-700 disabled:opacity-50 transition-colors">
        {groupsMutation.isPending ? 'Saving…' : 'Save Group Settings'}
      </button>
    </div>
  )
}
