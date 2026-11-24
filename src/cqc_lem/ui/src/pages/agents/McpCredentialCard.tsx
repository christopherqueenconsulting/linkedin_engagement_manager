import { useState } from 'react'
import { Link } from 'react-router-dom'
import SettingsCard from '../../components/SettingsCard'
import type { McpMintRequest } from '../../hooks/useMcpCredentials'
import { LABEL_PATTERN, clientInfo, defaultLabel } from './clientSnippets'
import type { ClientId } from './clientSnippets'

const TTL_OPTIONS = [7, 30, 90] as const
const DEFAULT_TTL = 30

const INPUT_CLASS =
  'w-full border border-field rounded-control px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-accent'

/** Where the factor enrolment lives (AuthFactorsCard, in Settings → Account & Billing). */
const ENROL_PATH = '/account?section=billing'

/**
 * Create one MCP credential (issue #2687, plan §6 card 1).
 *
 * The label is prefilled from the client tab and the device so the connections list can tell two
 * laptops apart — until the user types their own, after which a tab change leaves it alone. The
 * outreach scope is OFF by default (owner ruling O11): it is the one that opens DMs, leads and
 * other people's messages to the agent, so it has to be a choice rather than a default.
 */
export default function McpCredentialCard({
  client,
  hasStrongFactor,
  busy,
  error,
  onCreate,
}: {
  client: ClientId
  /** null while unknown — the server is the gate, so unknown still offers Create. */
  hasStrongFactor: boolean | null
  busy: boolean
  error: string | null
  onCreate: (request: McpMintRequest) => void
}) {
  const [typedLabel, setTypedLabel] = useState<string | null>(null)
  const [ttlDays, setTtlDays] = useState<McpMintRequest['ttl_days']>(DEFAULT_TTL)
  const [outreach, setOutreach] = useState(false)

  const suggested = defaultLabel(client, typeof navigator === 'undefined' ? '' : navigator.userAgent)
  const label = typedLabel ?? suggested
  const labelOk = LABEL_PATTERN.test(label) && label.trim().length > 0 && label.length <= 120

  return (
    <SettingsCard
      title="Create a connection"
      subtitle={`One token per agent and device. It is shown once, inside ready-to-paste commands for ${
        clientInfo(client).label}.`}
    >
      {hasStrongFactor === false ? (
        <div className="rounded-panel border border-warning/50 bg-warning/5 p-3 space-y-2" role="status">
          <p className="text-sm text-warning">
            An agent connection can read your LEM account, so creating one needs a passkey or an
            authenticator app on your account — an emailed code is not enough.
          </p>
          <Link
            to={ENROL_PATH}
            className="inline-flex bg-accent text-on-accent px-3 py-1.5 rounded-control text-xs font-semibold
              hover:bg-accent/90"
          >
            Enrol a passkey or authenticator first
          </Link>
        </div>
      ) : (
        <form
          className="space-y-3"
          onSubmit={(e) => {
            e.preventDefault()
            if (!labelOk || busy) return
            onCreate({ label: label.trim(), ttl_days: ttlDays, outreach })
          }}
        >
          <div className="grid gap-3 sm:grid-cols-[1fr_10rem]">
            <label className="block text-xs font-medium text-fg">
              Name
              <input
                type="text"
                value={label}
                onChange={(e) => setTypedLabel(e.target.value)}
                maxLength={120}
                aria-invalid={!labelOk}
                aria-describedby="mcp-label-help"
                className={`${INPUT_CLASS} mt-1 font-normal`}
              />
            </label>
            <label className="block text-xs font-medium text-fg">
              Expires after
              <select
                value={ttlDays}
                onChange={(e) => setTtlDays(Number(e.target.value) as McpMintRequest['ttl_days'])}
                className={`${INPUT_CLASS} mt-1 font-normal bg-raised`}
              >
                {TTL_OPTIONS.map((d) => (
                  <option key={d} value={d}>{d} days</option>
                ))}
              </select>
            </label>
          </div>
          <p id="mcp-label-help" className={`text-xs ${labelOk ? 'text-fg-muted' : 'text-danger'}`}>
            {labelOk
              ? 'Letters, numbers, spaces and . _ : - only.'
              : 'Use letters, numbers, spaces and . _ : - only (up to 120 characters).'}
          </p>
          <label className="flex items-start gap-2 text-sm text-fg">
            <input
              type="checkbox"
              checked={outreach}
              onChange={(e) => setOutreach(e.target.checked)}
              className="mt-1"
            />
            <span>
              Include outreach and leads (PII)
              <span className="block text-xs text-fg-muted">
                Lets the agent read your DMs, connection requests, leads and the messages people sent
                you. Leave this off unless the agent needs them.
              </span>
            </span>
          </label>
          {error && <p className="text-sm text-danger" role="alert">{error}</p>}
          <button
            type="submit"
            disabled={busy || !labelOk}
            className="bg-accent text-on-accent px-4 py-2 rounded-control text-sm font-semibold hover:bg-accent/90
              disabled:opacity-50"
          >
            {busy ? 'Creating…' : 'Create connection'}
          </button>
        </form>
      )}
    </SettingsCard>
  )
}
