import { useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import api from '../../api/client'
import { useAuth } from '../../contexts/AuthContext'
import Toggle from '../../components/Toggle'
import type { UserGroup } from './types'
import { useRegisterSaveSection, sectionSaveCallbacks } from './SettingsSaveContext'

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
      setGroups(groupsData)
      setSavedSig(JSON.stringify(groupsData))
      setGroupsInit(true)
    }
  }, [groupsData, groupsInit])

  const toggleGroup = (gid: string) =>
    setGroups((gs) => gs.map((g) => (g.group_id === gid ? { ...g, enabled: !g.enabled } : g)))

  const groupsMutation = useMutation({
    mutationFn: () =>
      api.put('/user/groups', {
        session_token: sessionToken,
        groups: Object.fromEntries(groups.map((g) => [g.group_id, g.enabled])),
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

  return (
    <div className="bg-white rounded-lg shadow-sm border border-gray-200 p-6 space-y-4">
      <h2 className="text-base font-semibold text-gray-700">LinkedIn Groups</h2>
      <p className="text-xs text-gray-500">Choose which of your joined groups LEM engages in (value-add comments + occasional posts). All on by default.</p>
      <div className="divide-y divide-gray-100">
        {groups.map((g) => (
          <div key={g.group_id} className="flex items-center justify-between py-2">
            <span className="text-sm text-gray-700 truncate pr-3">{g.group_name || `Group ${g.group_id}`}</span>
            <Toggle on={g.enabled} onClick={() => toggleGroup(g.group_id)} />
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
