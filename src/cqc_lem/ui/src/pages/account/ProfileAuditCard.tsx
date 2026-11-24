import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import axios from 'axios'
import api from '../../api/client'
import TableScroll from '../../components/TableScroll'
import { useAuth } from '../../contexts/useAuth'
import type { GetDetail } from '../../api/types'

// The Profile Audit card (issue #2753, Phase 2 of #2631).
//
// Reads `GET /user/profile-audit`: the score trend, the latest audit's findings, and the audit set
// beside follower and view readings. LEM never edits the LinkedIn profile: a finding is advice, and
// "Mark done" and "Ignore" only record what the user says. The audience table is a side-by-side, not
// a claim that the audit moved the numbers. Behind the profile-audit flag the server answers 404 and
// the card renders nothing; any other failure says so, since "unavailable" is not "no findings".

type AuditResponse = GetDetail<'/api/user/profile-audit'>
type Finding = NonNullable<AuditResponse['latest']>['findings'][number]

const UNMEASURED = 'Not measured'
const STATE_LABEL: Record<Finding['state'], string> = {
  open: 'Open',
  done: 'Marked done',
  ignored: 'Ignored',
  passing: 'Passing',
  unknown: 'Could not read',
}

function num(value: number | null | undefined, signed = false): string {
  if (value === null || value === undefined) return UNMEASURED
  return signed && value > 0 ? `+${value}` : String(value)
}

function Trend({ points }: { points: AuditResponse['trend'] }) {
  if (points.length === 0) return null
  return (
    <ol aria-label="Score trend" className="flex flex-wrap gap-2 text-xs">
      {points.map((p) => (
        <li key={p.period_key} data-testid="audit-trend-point"
          className="rounded-md border border-gray-200 px-2 py-1">
          <span className="text-gray-500">{p.period_key}</span>{' '}
          <span className="font-semibold text-gray-800">{p.score === null ? UNMEASURED : `${p.score}/100`}</span>
        </li>
      ))}
    </ol>
  )
}

function FindingRow({
  finding, busy, onAct,
}: {
  finding: Finding
  busy: boolean
  onAct: (checkId: string, action: 'done' | 'ignored' | 'reopen') => void
}) {
  const actionable = finding.status === 'warn' || finding.status === 'fail'
  return (
    <li data-testid="audit-finding" className="border border-gray-200 rounded-lg p-3 space-y-1">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex items-center gap-2 text-xs">
          <span className="font-medium text-gray-800">{finding.check_id.replace(/_/g, ' ')}</span>
          <span className="px-2 py-0.5 rounded-full bg-gray-100 text-gray-700">{STATE_LABEL[finding.state]}</span>
          {finding.delta === 'new' && <span className="text-amber-700">New since the last audit</span>}
          {finding.delta === 'closed' && <span className="text-green-700">Fixed since the last audit</span>}
        </div>
        {actionable && (
          <div className="flex gap-2">
            {finding.state === 'open' ? (
              <>
                <button type="button" disabled={busy}
                  onClick={() => onAct(finding.check_id, 'done')}
                  aria-label={`Mark done: ${finding.check_id.replace(/_/g, ' ')}`}
                  className="px-3 py-1 text-xs font-medium rounded-md border border-gray-300 text-gray-700 hover:bg-gray-50">
                  Mark done
                </button>
                <button type="button" disabled={busy}
                  onClick={() => onAct(finding.check_id, 'ignored')}
                  aria-label={`Ignore: ${finding.check_id.replace(/_/g, ' ')}`}
                  className="px-3 py-1 text-xs font-medium rounded-md border border-gray-300 text-gray-700 hover:bg-gray-50">
                  Ignore
                </button>
              </>
            ) : (
              <button type="button" disabled={busy}
                onClick={() => onAct(finding.check_id, 'reopen')}
                aria-label={`Reopen: ${finding.check_id.replace(/_/g, ' ')}`}
                className="px-3 py-1 text-xs font-medium rounded-md border border-gray-300 text-gray-700 hover:bg-gray-50">
                Reopen
              </button>
            )}
          </div>
        )}
      </div>
      <p className="text-sm text-gray-700">{finding.detail}</p>
      {actionable && finding.state === 'open' && <p className="text-xs text-gray-500">{finding.fix_hint}</p>}
    </li>
  )
}

export default function ProfileAuditCard() {
  const { sessionToken } = useAuth()
  const queryClient = useQueryClient()
  const [failed, setFailed] = useState(false)
  const key = ['profile-audit', sessionToken]

  const { data, error, isError } = useQuery({
    queryKey: key,
    queryFn: () =>
      api
        .get(`/user/profile-audit?session_token=${encodeURIComponent(sessionToken!)}`)
        .then((r) => r.data.detail as AuditResponse),
    enabled: !!sessionToken,
    staleTime: 60 * 1000,
    retry: false,
  })

  const act = useMutation({
    mutationFn: (v: { checkId: string; action: 'done' | 'ignored' | 'reopen' }) =>
      api.post('/user/profile-audit/finding', {
        session_token: sessionToken, check_id: v.checkId, action: v.action,
      }),
    onSuccess: () => {
      setFailed(false)
      return queryClient.invalidateQueries({ queryKey: key })
    },
    onError: () => setFailed(true),
  })

  // Flag off for this account: the server says 404 and there is nothing to show.
  if (isError && axios.isAxiosError(error) && error.response?.status === 404) return null
  if (isError) {
    return (
      <div data-testid="profile-audit" className="bg-white rounded-lg shadow-sm border border-gray-200 p-6">
        <h2 className="text-base font-semibold text-gray-700">Profile audit</h2>
        <p role="status" className="text-sm text-gray-500 mt-2">The profile audit is temporarily unavailable.</p>
      </div>
    )
  }
  if (!data) return null

  const latest = data.latest
  const rows = data.correlation.rows
  return (
    <div data-testid="profile-audit" className="bg-white rounded-lg shadow-sm border border-gray-200 p-6 space-y-4">
      <div>
        <h2 className="text-base font-semibold text-gray-700">Profile audit</h2>
        <p className="text-xs text-gray-500 mt-1">
          LEM grades your saved LinkedIn profile each week and never edits it. Make the changes
          yourself on LinkedIn.
        </p>
      </div>
      {!latest ? (
        <p className="text-sm text-gray-500">No audit yet. The first one runs on the next weekly check.</p>
      ) : (
        <>
          <p className="text-sm text-gray-800">
            <span className="text-2xl font-semibold">{latest.score === null ? UNMEASURED : `${latest.score}/100`}</span>{' '}
            <span data-testid="audit-open-count">{latest.open_count} open {latest.open_count === 1 ? 'finding' : 'findings'}</span>
          </p>
          <Trend points={data.trend} />
          <ul className="space-y-2">
            {latest.findings.map((f) => (
              <FindingRow key={f.check_id} finding={f} busy={act.isPending}
                onAct={(checkId, action) => act.mutate({ checkId, action })} />
            ))}
          </ul>
          {failed && <p role="alert" className="text-sm text-red-600">Could not save that. Try again shortly.</p>}
          {rows.length > 0 && (
            <div className="space-y-1">
              <h3 className="text-sm font-medium text-gray-700">Beside your audience</h3>
              <p className="text-xs text-gray-500">{data.correlation.note}</p>
              <TableScroll label="Audit score beside audience" minWidth={480} testId="audit-correlation-table">
              <table className="text-xs w-full">
                <thead>
                  <tr className="text-left text-gray-500">
                    <th scope="col">Audit</th><th scope="col">Score</th><th scope="col">Open</th>
                    <th scope="col">Followers</th><th scope="col">Change</th><th scope="col">Profile views</th>
                  </tr>
                </thead>
                <tbody>
                  {rows.map((r) => (
                    <tr key={r.period_key} data-testid="audit-correlation-row">
                      <td>{r.period_key}</td>
                      <td>{num(r.score)}</td>
                      <td>{r.open_count}</td>
                      <td>{num(r.follower_count)}</td>
                      <td>{num(r.follower_change, true)}</td>
                      <td>{num(r.profile_views)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
              </TableScroll>
            </div>
          )}
        </>
      )}
    </div>
  )
}
