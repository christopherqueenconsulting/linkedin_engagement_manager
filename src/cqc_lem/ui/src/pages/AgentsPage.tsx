import { useEffect, useRef, useState } from 'react'
import { Navigate } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import api from '../api/client'
import { useAuth } from '../contexts/useAuth'
import { FLAGS, useFeatureFlags } from '../hooks/useFeatureFlags'
import { errorCode, errorStatus, rowLabel, useMcpCredentials } from '../hooks/useMcpCredentials'
import type { McpMintRequest, McpMintResponse, McpTokenRow } from '../hooks/useMcpCredentials'
import { useStepUp } from '../hooks/useStepUp'
import {
  recordMcpCommandCopied,
  recordMcpTokenMinted,
  recordMcpTokenRevoked,
  recordMcpTokenRotated,
} from '../utils/analytics'
import ConnectCommands from './agents/ConnectCommands'
import ConnectedAgentsCard from './agents/ConnectedAgentsCard'
import McpCredentialCard from './agents/McpCredentialCard'
import { AgentPromptCard, StarterPromptsCard } from './agents/PromptCards'
import type { ClientId } from './agents/clientSnippets'

const STEP_UP_REASON =
  'An agent connection can read your LEM account, so creating or changing one needs one more check.'

const BANNER_CLASS = 'rounded-panel border border-warning/50 bg-warning/5 px-4 py-3 text-sm text-warning'

/** The token, for the show-once window only. Component state is its ONLY home (plan §6). */
type Shown = { token: string; mcpUrl: string | null }

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
 * Flag-gated twice over — the nav hides the entry and this route sends a direct visit home — and
 * the server answers 404 behind both, so the flag is a rollout control, never the safety one.
 */
export default function AgentsPage() {
  const flags = useFeatureFlags()
  // A flags payload still in flight must not bounce someone off a page they are allowed on.
  if (flags.isLoading) return <div className="h-40" aria-busy="true" />
  if (!flags.data?.flags[FLAGS.mcpServer]) return <Navigate to="/" replace />
  return <AgentsContent />
}

function AgentsContent() {
  const { sessionToken } = useAuth()
  const { info, refresh, mint, rotate, revoke } = useMcpCredentials()
  const { guard, stepUpModal } = useStepUp({ reason: STEP_UP_REASON })
  const [client, setClient] = useState<ClientId>('claude-code')
  const [shown, setShown] = useState<Shown | null>(null)
  const [creating, setCreating] = useState(false)
  const [mintError, setMintError] = useState<string | null>(null)
  const [needsFactor, setNeedsFactor] = useState(false)
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
    enabled: !!sessionToken,
    staleTime: 30 * 1000,
  })
  const hasStrongFactor = needsFactor ? false : (factors.data?.has_strong_factor ?? null)

  // Move focus to the show-once panel when a token arrives, so a keyboard or screen-reader user
  // lands on the thing that will not be there later.
  useEffect(() => {
    if (shown) tokenHeading.current?.focus()
  }, [shown])

  function show(result: McpMintResponse) {
    setShown({ token: result.session_token, mcpUrl: result.mcp_url })
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
      const { message, needsFactor: factor } = mintErrorMessage(e)
      setMintError(message)
      if (factor) setNeedsFactor(true)
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
      // re-read the list rather than show a row that may already be dead.
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
      recordMcpTokenRevoked(row.scope)
      setRowMsg({ ok: true, text: `Revoked ${rowLabel(row)}. That agent is disconnected.` })
      refresh()
    } catch {
      setRowMsg({ ok: false, text: 'Could not revoke that connection. Please try again.' })
    } finally {
      setRowBusy(null)
    }
  }

  // The URL is always the server's; a freshly minted response carries one even when the info
  // query has not answered.
  const url = info.data?.mcp_url ?? shown?.mcpUrl ?? null

  return (
    <div className="max-w-4xl mx-auto space-y-6">
      {stepUpModal}
      <div>
        <h1 className="text-section lg:text-section-lg font-semibold text-fg">Agents</h1>
        <p className="mt-2 text-sm text-fg-muted max-w-2xl">
          Connect a coding agent such as Claude Code, Codex or Cursor to your LEM account over MCP. It
          can read your content, plan and results. Anything it creates waits here for your approval —
          it can never approve or send on its own.
        </p>
      </div>

      {info.data?.enabled === false && (
        <div role="status" className={BANNER_CLASS}>
          Agent connections are switched off right now. Connected agents will be refused until they
          are back on — nothing you set up here is lost.
        </div>
      )}

      <McpCredentialCard
        client={client}
        hasStrongFactor={hasStrongFactor}
        busy={creating}
        error={mintError}
        onCreate={create}
      />

      {url ? (
        <ConnectCommands
          ref={tokenHeading}
          url={url}
          token={shown?.token ?? null}
          client={client}
          onClientChange={setClient}
          onCopied={(c, variant) => recordMcpCommandCopied(c, variant)}
          onHideToken={() => setShown(null)}
        />
      ) : info.data ? (
        // Answered, but with no URL: the server has no public address configured, so there is
        // nothing a command could point at. Say so rather than spin.
        <div role="status" className="bg-raised rounded-panel border border-hairline p-6">
          <p className="text-sm text-fg">
            Connect commands are unavailable right now: LEM has no public address to give your agent.
            Please try again later.
          </p>
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
      {rowMsg && (
        <p role="status" className={`text-sm font-medium ${rowMsg.ok ? 'text-success' : 'text-danger'}`}>
          {rowMsg.text}
        </p>
      )}
    </div>
  )
}
