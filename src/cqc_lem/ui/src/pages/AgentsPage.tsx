import { useEffect, useRef, useState } from 'react'
import { Navigate } from 'react-router-dom'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import api from '../api/client'
import { useAuth } from '../contexts/useAuth'
import { FLAGS, useFeatureFlags } from '../hooks/useFeatureFlags'
import { errorCode, errorStatus, isLive, rowLabel, useMcpCredentials } from '../hooks/useMcpCredentials'
import type { McpMintRequest, McpMintResponse, McpTokenRow } from '../hooks/useMcpCredentials'
import { useStepUp } from '../hooks/useStepUp'
import {
  recordMcpCommandCopied,
  recordMcpTokenMinted,
  recordMcpTokenRevoked,
  recordMcpTokenRotated,
} from '../utils/analytics'
import AgentActivityCard from './agents/AgentActivityCard'
import AgentChangeRequestsCard from './agents/AgentChangeRequestsCard'
import ConnectCommands from './agents/ConnectCommands'
import ConnectedAgentsCard from './agents/ConnectedAgentsCard'
import McpCredentialCard from './agents/McpCredentialCard'
import { AgentPromptCard, StarterPromptsCard } from './agents/PromptCards'
import ShownTokenPanel from './agents/ShownTokenPanel'
import type { ClientId } from './agents/clientSnippets'

const STEP_UP_REASON =
  'An agent connection can read your LEM account, so creating or changing one needs one more check.'

const BANNER_CLASS = 'rounded-panel border border-warning/50 bg-warning/5 px-4 py-3 text-sm text-warning'

const NO_URL =
  'LEM has no public address to give your agent right now, so a new connection could not be used. ' +
  'Please try again later.'

/**
 * The token, for the show-once window only. Component state is its ONLY home (plan §6).
 * `sessionId` ties it to its row, so revoking that row — or a rotate of it that may have revoked
 * it server-side — takes the token off the screen with it.
 */
type Shown = { token: string; mcpUrl: string | null; sessionId: number }

function mintErrorMessage(
  error: unknown,
  fallback = 'Could not create the connection. Please try again.',
): { message: string; needsFactor: boolean } {
  const code = errorCode(error)
  const status = errorStatus(error)
  if (code === 'mcp_needs_strong_factor') {
    return { message: 'Enrol a passkey or authenticator app first.', needsFactor: true }
  }
  if (code === 'mcp_token_limit' || status === 409) {
    return { message: 'You have reached the limit of agent connections. Revoke one first.', needsFactor: false }
  }
  if (status === 404) {
    return { message: 'Agent connections are not switched on for your account yet.', needsFactor: false }
  }
  if (status === 429) {
    return { message: 'Too many new connections in the last hour. Try again later.', needsFactor: false }
  }
  return { message: fallback, needsFactor: false }
}

/**
 * The Agents tab (issue #2687, docs/agent-mcp-plan.md §6): mint an MCP token under step-up, paste
 * a per-client command, and manage the connections that exist.
 *
 * The rollout flag decides what this page OFFERS, never whether someone can turn off what they
 * already have: flag off with a live token renders a read-only list that can still revoke it, and
 * only flag off with nothing live sends a direct visit home. The server answers 404 to a mint
 * behind it either way — the flag is a rollout control, never the safety one.
 */
export default function AgentsPage() {
  const flags = useFeatureFlags()
  // A flags payload still in flight must not bounce someone off a page they are allowed on.
  if (flags.isLoading) return <div className="h-40" aria-busy="true" />
  return <AgentsContent readOnly={!flags.data?.flags[FLAGS.mcpServer]} />
}

function AgentsContent({ readOnly }: { readOnly: boolean }) {
  const { sessionToken } = useAuth()
  const queryClient = useQueryClient()
  const { info, refresh, mint, rotate, revoke } = useMcpCredentials()
  const { guard, stepUpModal } = useStepUp({ reason: STEP_UP_REASON })
  const [client, setClient] = useState<ClientId>('claude-code')
  const [shown, setShown] = useState<Shown | null>(null)
  const [creating, setCreating] = useState(false)
  const [mintError, setMintError] = useState<string | null>(null)
  // When the server last refused for want of a strong factor. It outranks the factors read only
  // until a FRESHER read arrives — enrolling elsewhere and coming back must bring Create back.
  const [factorRefusedAt, setFactorRefusedAt] = useState<number | null>(null)
  const [rowBusy, setRowBusy] = useState<number | null>(null)
  const [rowMsg, setRowMsg] = useState<{ ok: boolean; text: string } | null>(null)
  const tokenHeading = useRef<HTMLHeadingElement>(null)

  // Same key and shape as AuthFactorsCard, so the two share one cached answer.
  const factors = useQuery({
    queryKey: ['auth-factors', sessionToken],
    queryFn: () =>
      api
        .get(`/user/auth-factors?session_token=${encodeURIComponent(sessionToken!)}`)
        .then((r) => r.data.detail as { has_strong_factor: boolean }),
    enabled: !!sessionToken && !readOnly,
    staleTime: 30 * 1000,
  })
  const refusalStands = factorRefusedAt !== null && factors.dataUpdatedAt <= factorRefusedAt
  const hasStrongFactor = refusalStands ? false : (factors.data?.has_strong_factor ?? null)

  // Move focus to the show-once panel when a token arrives, so a keyboard or screen-reader user
  // lands on the thing that will not be there later.
  useEffect(() => {
    if (shown) tokenHeading.current?.focus()
  }, [shown])

  function show(result: McpMintResponse) {
    setShown({ token: result.session_token, mcpUrl: result.mcp_url, sessionId: result.session_id })
  }

  async function create(request: McpMintRequest) {
    setCreating(true)
    setMintError(null)
    try {
      const result = await guard(() => mint(request))
      if (result === null) return // step-up cancelled: nothing changes
      show(result)
      recordMcpTokenMinted(request.ttl_days, result.scope)
      refresh()
    } catch (e) {
      const { message, needsFactor } = mintErrorMessage(e)
      setMintError(message)
      if (needsFactor) {
        setFactorRefusedAt(Date.now())
        queryClient.invalidateQueries({ queryKey: ['auth-factors'] })
      }
    } finally {
      setCreating(false)
    }
  }

  async function onRotate(row: McpTokenRow) {
    setRowBusy(row.session_id)
    setRowMsg(null)
    try {
      const result = await guard(() => rotate(row.session_id))
      if (result === null) return
      show(result)
      recordMcpTokenRotated(result.scope)
      setRowMsg({ ok: true, text: `Rotated ${rowLabel(row)}. The old token no longer works.` })
      refresh()
    } catch (e) {
      // The server revokes the old token BEFORE minting, so a failure part-way can leave none:
      // drop it from the screen if it is the one shown, and re-read the list.
      setShown((s) => (s?.sessionId === row.session_id ? null : s))
      const text = errorStatus(e) === 404 && errorCode(e) !== 'mcp_needs_strong_factor'
        ? 'That connection is no longer live. Create a new one above.'
        : mintErrorMessage(e, 'Could not rotate that connection. Please try again.').message
      setRowMsg({ ok: false, text })
      refresh()
    } finally {
      setRowBusy(null)
    }
  }

  async function onRevoke(row: McpTokenRow) {
    setRowBusy(row.session_id)
    setRowMsg(null)
    try {
      const result = await guard(async () => {
        await revoke(row.session_id)
        return true
      })
      if (result === null) return
      setShown((s) => (s?.sessionId === row.session_id ? null : s))
      recordMcpTokenRevoked(row.scope)
      setRowMsg({ ok: true, text: `Revoked ${rowLabel(row)}. That agent is disconnected.` })
      refresh()
    } catch {
      setRowMsg({ ok: false, text: 'Could not revoke that connection. Please try again.' })
    } finally {
      setRowBusy(null)
    }
  }

  const header = (
    <div>
      <h1 className="text-section lg:text-section-lg font-semibold text-fg">Agents</h1>
      <p className="mt-2 text-sm text-fg-muted max-w-2xl">
        Connect a coding agent such as Claude Code, Codex or Cursor to your LEM account over MCP. It
        can read your content, plan and results. Anything it creates waits here for your approval —
        it can never approve or send on its own.
      </p>
    </div>
  )
  const rowStatus = rowMsg && (
    <p role="status" className={`text-sm font-medium ${rowMsg.ok ? 'text-success' : 'text-danger'}`}>
      {rowMsg.text}
    </p>
  )
  // Says what the server reported, and nothing about why.
  const offBanner = (
    <div role="status" className={BANNER_CLASS}>
      Agent connections are turned off for your account, so connected agents cannot reach LEM right now.
      You can still revoke the connections below.
    </div>
  )

  if (readOnly) {
    if (info.isLoading) return <div className="h-40" aria-busy="true" />
    // Nothing live to turn off (or no answer to say otherwise): there is nothing for this page to do.
    if (!info.data || !info.data.tokens.some(isLive)) return <Navigate to="/" replace />
    return (
      <div className="max-w-4xl mx-auto space-y-6">
        {stepUpModal}
        {header}
        {offBanner}
        <AgentChangeRequestsCard />
        <ConnectedAgentsCard
          tokens={info.data.tokens}
          busyId={rowBusy}
          onRotate={onRotate}
          onRevoke={onRevoke}
          canRotate={false}
        />
        {rowStatus}
      </div>
    )
  }

  // The URL is always the server's; a freshly minted response carries one even when the info
  // query has not answered.
  const url = info.data?.mcp_url ?? shown?.mcpUrl ?? null
  // Answered, but with nothing to connect to: refuse to mint a token nobody could use.
  const noUrl = !!info.data && !info.data.mcp_url

  return (
    <div className="max-w-4xl mx-auto space-y-6">
      {stepUpModal}
      {header}

      {info.data?.enabled === false && offBanner}

      <AgentChangeRequestsCard />

      <McpCredentialCard
        client={client}
        hasStrongFactor={hasStrongFactor}
        unavailable={noUrl ? NO_URL : null}
        busy={creating}
        error={mintError}
        onCreate={create}
      />

      {/* On its own, never inside the commands: a token the server minted is always shown. */}
      {shown && (
        <ShownTokenPanel
          ref={tokenHeading}
          token={shown.token}
          onCopied={() => recordMcpCommandCopied(client, 'token')}
          onHide={() => setShown(null)}
        />
      )}

      {url ? (
        <ConnectCommands
          url={url}
          token={shown?.token ?? null}
          client={client}
          onClientChange={setClient}
          onCopied={(c, variant) => recordMcpCommandCopied(c, variant)}
        />
      ) : noUrl ? (
        <div role="status" className="bg-raised rounded-panel border border-hairline p-6">
          <p className="text-sm text-fg">Connect commands are unavailable right now. {NO_URL}</p>
        </div>
      ) : info.isError ? (
        <div role="alert" className="bg-raised rounded-panel border border-hairline p-6 space-y-2">
          <p className="text-sm text-fg">
            Could not load your agent connections, so the connect commands are hidden.
          </p>
          <button
            type="button"
            onClick={() => info.refetch()}
            className="text-sm font-semibold text-accent hover:underline"
          >
            Try again
          </button>
        </div>
      ) : (
        <div className="h-24" aria-busy="true" />
      )}

      <AgentPromptCard />
      <StarterPromptsCard />

      {info.data && (
        <ConnectedAgentsCard tokens={info.data.tokens} busyId={rowBusy} onRotate={onRotate} onRevoke={onRevoke} />
      )}
      {rowStatus}
      {info.data && info.data.tokens.length > 0 && <AgentActivityCard />}
    </div>
  )
}
