import { useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import api from '../../api/client'
import { useAuth } from '../../contexts/AuthContext'
import Toggle from '../../components/Toggle'
import type { UserGroup } from './types'
import { useRegisterSaveSection, sectionSaveCallbacks } from './SettingsSaveContext'

const groupLabel = (g: UserGroup) => g.group_name || `Group ${g.group_id}`

export default function GroupsCard() {
  const { sessionToken } = useAuth()
  const queryClient = useQueryClient()
  const [groups, setGroups] = useState<UserGroup[]>([])
  const [groupsInit, setGroupsInit] = useState(false)
  const [savedSig, setSavedSig] = useState<string | null>(null)
  const [groupsMsg, setGroupsMsg] = useState<{ ok: boolean; text: string } | null>(null)

  const { data: groupsData } = useQuery({
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
      // A group synced before #769 has no post_enabled in an older cached payload — treat a missing
      // flag as ON, which is what the column defaults to server-side.
      const normalized = groupsData.map((g) => ({ ...g, post_enabled: g.post_enabled !== false }))
      setGroups(normalized)
      setSavedSig(JSON.stringify(normalized))
      setGroupsInit(true)
    }
  }, [groupsData, groupsInit])

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
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['user-groups'] })
      setSavedSig(JSON.stringify(groups))
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

  if (!(groupsInit && groups.length > 0)) return null

  // The server decides the rotation (least-recently-posted first) and marks the row; we only hide
  // the badge when this row's own toggles rule it out, so an unsaved edit never shows a
  // contradiction. The real next group is re-resolved on save.
  const markedNext = groups.find((g) => g.is_next_post && g.post_enabled)
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
          on has gone longest without one.
        </p>
      </div>

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
