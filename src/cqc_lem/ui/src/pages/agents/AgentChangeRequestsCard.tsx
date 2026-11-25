import { useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import api from '../../api/client'
import type { ApiEnvelope } from '../../api/types'
import SettingsCard from '../../components/SettingsCard'
import { useAuth } from '../../contexts/useAuth'
import { useStepUp } from '../../hooks/useStepUp'
import { SETTINGS_PROPOSALS_KEY, formatValue } from './changeRequests'
import type { Decision, Proposal } from './changeRequests'

// "Changes your agent asked for" (epic #2797, issue #2803). An agent may ASK to loosen a safety or
// approval gate; only the signed-in person decides it here. Approving applies the chosen fields
// behind a fresh step-up; rejecting loosens nothing.
//
// The agent's `reason` is UNTRUSTED text. It is rendered only as a React text child inside a quote,
// never through innerHTML, never as a link and never as a label for a button, and it sits under a
// line saying who wrote it. LEM's own warning for each field is separate and comes from the server.

const STEP_UP_REASON =
  'Approving a change request loosens a safety setting, so it needs one more check.'

function when(value: string | null): string {
  if (!value) return '—'
  const d = new Date(value)
  return Number.isNaN(d.getTime()) ? '—' : d.toLocaleDateString()
}

function labelOf(proposal: Proposal, field: string): string {
  return proposal.fields.find((f) => f.field === field)?.label ?? field
}

function errorText(error: unknown): string {
  const response = (error as { response?: { status?: number; data?: { detail?: unknown } } })?.response
  const detail = response?.data?.detail
  const message = (detail as { message?: unknown } | undefined)?.message
  if (response?.status === 409 && typeof message === 'string') return message
  if (response?.status === 404) return 'That request is no longer there. It may already be decided.'
  return 'Could not save your decision. Nothing was changed. Please try again.'
}

function ProposalBlock({
  proposal,
  busy,
  onApprove,
  onReject,
}: {
  proposal: Proposal
  busy: boolean
  onApprove: (proposal: Proposal, fields: string[]) => void
  onReject: (proposal: Proposal) => void
}) {
  // Every field starts selected; unticking one leaves it out of the approval.
  const [off, setOff] = useState<Set<string>>(new Set())
  const chosen = proposal.fields.filter((f) => !off.has(f.field)).map((f) => f.field)

  function toggle(field: string) {
    setOff((current) => {
      const next = new Set(current)
      if (next.has(field)) next.delete(field)
      else next.add(field)
      return next
    })
  }

  return (
    <li data-testid={`proposal-${proposal.id}`} className="border border-hairline rounded-panel p-4 space-y-3">
      <p className="text-xs text-fg-muted">
        Asked {when(proposal.created_at)} · Expires {when(proposal.expires_at)}
      </p>

      {proposal.reason && (
        <figure className="space-y-1">
          <figcaption className="text-xs text-fg-muted">
            What your agent said (written by the agent, not by LEM):
          </figcaption>
          <blockquote
            data-testid="proposal-reason"
            className="border-l-2 border-hairline pl-3 text-sm text-fg break-words whitespace-pre-wrap"
          >
            {proposal.reason}
          </blockquote>
        </figure>
      )}

      <ul className="space-y-3">
        {proposal.fields.map((f) => {
          const id = `proposal-${proposal.id}-${f.field}`
          return (
            <li key={f.field} className="space-y-1">
              <label htmlFor={id} className="flex items-start gap-3 min-h-11 cursor-pointer">
                <input
                  id={id}
                  type="checkbox"
                  checked={!off.has(f.field)}
                  onChange={() => toggle(f.field)}
                  disabled={busy}
                  className="mt-1 h-5 w-5 shrink-0"
                />
                <span className="min-w-0">
                  <span className="block text-sm font-semibold text-fg">{f.label}</span>
                  <span className="block text-sm text-fg">
                    {formatValue(f.before)} <span aria-label="changes to">→</span> {formatValue(f.requested)}
                  </span>
                </span>
              </label>
              <p
                className={`ml-8 rounded-control px-3 py-2 text-sm ${
                  f.loosens ? 'border border-warning/50 bg-warning/5 text-warning' : 'text-fg-muted'
                }`}
              >
                {f.warning}
              </p>
            </li>
          )
        })}
      </ul>

      <div className="flex flex-wrap items-center gap-3 pt-1">
        <button
          type="button"
          onClick={() => onApprove(proposal, chosen)}
          disabled={busy || chosen.length === 0}
          className="min-h-11 bg-accent text-white px-4 rounded-panel text-sm font-semibold hover:bg-accent/90 disabled:opacity-50"
        >
          {chosen.length === proposal.fields.length ? 'Approve all' : `Approve selected (${chosen.length})`}
        </button>
        <button
          type="button"
          onClick={() => onReject(proposal)}
          disabled={busy}
          className="min-h-11 px-4 rounded-panel border border-field text-sm font-semibold text-fg hover:bg-page disabled:opacity-50"
        >
          Reject all
        </button>
      </div>
    </li>
  )
}

export default function AgentChangeRequestsCard() {
  const { sessionToken } = useAuth()
  const queryClient = useQueryClient()
  const { guard, stepUpModal } = useStepUp({ reason: STEP_UP_REASON })
  const [busyId, setBusyId] = useState<number | null>(null)
  const [message, setMessage] = useState<{ ok: boolean; text: string } | null>(null)

  const proposals = useQuery({
    queryKey: [SETTINGS_PROPOSALS_KEY, sessionToken],
    queryFn: () =>
      api
        .get<ApiEnvelope<{ proposals: Proposal[]; count: number }>>(
          `/user/settings-proposals?session_token=${encodeURIComponent(sessionToken!)}`)
        .then((r) => r.data.detail.proposals),
    enabled: !!sessionToken,
    staleTime: 30 * 1000,
    retry: 1,
  })

  const refresh = () => queryClient.invalidateQueries({ queryKey: [SETTINGS_PROPOSALS_KEY] })

  async function approve(proposal: Proposal, fields: string[]) {
    setBusyId(proposal.id)
    setMessage(null)
    try {
      const sent = fields.length === proposal.fields.length ? undefined : fields
      const result = await guard(() =>
        api.post<ApiEnvelope<Decision>>(`/user/settings-proposals/${proposal.id}/approve`, {
          session_token: sessionToken,
          fields: sent,
        }),
      )
      if (result === null) return // step-up cancelled: nothing changed
      const d = result.data.detail
      const names = (list: string[]) => list.map((f) => labelOf(proposal, f)).join(', ')
      const parts: string[] = []
      parts.push(d.applied.length ? `Applied: ${names(d.applied)}.` : 'Nothing was applied.')
      if (d.stale.length) {
        parts.push(`Skipped because it changed since your agent asked: ${names(d.stale)}.`)
      }
      if (d.declined.length) parts.push(`Left out: ${names(d.declined)}.`)
      setMessage({ ok: d.applied.length > 0, text: parts.join(' ') })
      refresh()
    } catch (e) {
      setMessage({ ok: false, text: errorText(e) })
      refresh()
    } finally {
      setBusyId(null)
    }
  }

  async function reject(proposal: Proposal) {
    setBusyId(proposal.id)
    setMessage(null)
    try {
      await api.post(`/user/settings-proposals/${proposal.id}/reject`, { session_token: sessionToken })
      setMessage({ ok: true, text: 'Rejected. Nothing was changed.' })
      refresh()
    } catch (e) {
      setMessage({ ok: false, text: errorText(e) })
      refresh()
    } finally {
      setBusyId(null)
    }
  }

  const list = proposals.data ?? []
  // Nothing asked and nothing to report: no card, so a user who never connects an agent never sees it.
  if (!proposals.isError && !message && (proposals.isLoading || list.length === 0)) return null

  return (
    <SettingsCard
      title="Changes your agent asked for"
      subtitle="Your agent can ask, but only you can approve. Nothing changes until you do."
    >
      {stepUpModal}
      {proposals.isError ? (
        <div role="alert" className="space-y-1">
          <p className="text-sm text-fg">Could not load your agent's requests.</p>
          <button
            type="button"
            onClick={() => proposals.refetch()}
            className="text-sm font-semibold text-accent hover:underline"
          >
            Try again
          </button>
        </div>
      ) : (
        <ul className="space-y-4">
          {list.map((p) => (
            <ProposalBlock key={p.id} proposal={p} busy={busyId === p.id} onApprove={approve} onReject={reject} />
          ))}
        </ul>
      )}
      {message && (
        <p role="status" className={`text-sm font-medium ${message.ok ? 'text-success' : 'text-danger'}`}>
          {message.text}
        </p>
      )}
    </SettingsCard>
  )
}
