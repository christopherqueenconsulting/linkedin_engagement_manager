import { useState } from 'react'
import CopyBlock from '../../components/CopyBlock'
import SettingsCard from '../../components/SettingsCard'
import { CLIENTS, agentSnippet, buildClientSnippets, clientInfo, defaultShell } from './clientSnippets'
import type { ClientId, ShellKind, SnippetVariant } from './clientSnippets'

const TAB_BASE = 'shrink-0 min-h-9 px-3 py-1.5 rounded-control text-xs font-semibold whitespace-nowrap border'
const SHELLS: { id: ShellKind; label: string }[] = [
  { id: 'posix', label: 'bash / zsh' },
  { id: 'powershell', label: 'PowerShell' },
]

/**
 * The per-client connect commands (issue #2687, plan §6 card 2).
 *
 * `token` is non-null ONLY inside the show-once window — the page holds it in component state and
 * nowhere else — and every block falls back to `<YOUR_TOKEN>` the moment it is cleared. The
 * primary variant types the token once, on an `export` / `$env:` line, and stores only the
 * variable's name wherever the client can expand it; the inline variant is behind a deliberate
 * toggle because it writes the secret into a config file or a command line. The shell defaults
 * from the browser's OS and can be switched, because the quoting differs.
 */
export default function ConnectCommands({
  url,
  token,
  client,
  onClientChange,
  onCopied,
}: {
  url: string
  token: string | null
  client: ClientId
  onClientChange: (client: ClientId) => void
  onCopied: (client: ClientId, variant: string) => void
}) {
  const [variant, setVariant] = useState<SnippetVariant>('env')
  const [shell, setShell] = useState<ShellKind>(() =>
    defaultShell(typeof navigator === 'undefined' ? '' : navigator.userAgent))
  const info = clientInfo(client)
  const agent = agentSnippet(client, url)
  const snippets = info.supported ? buildClientSnippets(client, url, token, variant, shell) : []

  return (
    <SettingsCard
      title="Connect your agent"
      subtitle="Pick your client, then run its command in a terminal on the machine the agent runs on."
    >
      <div role="tablist" aria-label="Agent client" className="flex gap-2 overflow-x-auto pb-1">
        {CLIENTS.map((c) => {
          const selected = c.id === client
          return (
            <button
              key={c.id}
              type="button"
              role="tab"
              id={`client-tab-${c.id}`}
              aria-selected={selected}
              aria-controls="client-panel"
              onClick={() => onClientChange(c.id)}
              className={`${TAB_BASE} ${selected
                ? 'bg-accent text-on-accent border-accent'
                : 'bg-raised text-fg-muted border-field hover:text-fg'}`}
            >
              {c.label}
            </button>
          )
        })}
      </div>

      <div id="client-panel" role="tabpanel" aria-labelledby={`client-tab-${client}`} className="space-y-3">
        {!info.supported ? (
          <p className="text-sm text-fg-muted">
            <span className="font-semibold text-fg">{info.unsupportedReason}.</span>{' '}
            {info.label} can only connect through OAuth, which LEM does not offer yet. Use one of the
            other clients for now.
          </p>
        ) : (
          <>
            {!token && (
              <p className="text-xs text-fg-muted">
                These show <code className="font-mono">&lt;YOUR_TOKEN&gt;</code> because a token is only
                shown once. Rotate a live connection below, or create a new one, to get a paste-ready command.
              </p>
            )}
            <div className="flex flex-wrap items-start gap-x-6 gap-y-3">
              <fieldset className="flex items-center gap-3 text-xs text-fg">
                <legend className="sr-only">Shell</legend>
                {SHELLS.map((s) => (
                  <label key={s.id} className="flex items-center gap-1.5">
                    <input
                      type="radio"
                      name="mcp-shell"
                      value={s.id}
                      checked={shell === s.id}
                      onChange={() => setShell(s.id)}
                    />
                    {s.label}
                  </label>
                ))}
              </fieldset>
              <label className="flex items-start gap-2 text-xs text-fg flex-1 min-w-[16rem]">
                <input
                  type="checkbox"
                  checked={variant === 'inline'}
                  onChange={(e) => setVariant(e.target.checked ? 'inline' : 'env')}
                  className="mt-0.5"
                />
                <span>
                  Show inline token
                  <span className="block text-fg-muted">
                    Writes the token straight into the command or file instead of an environment
                    variable. Still never into your agent's chat.
                  </span>
                </span>
              </label>
            </div>
            {snippets.map((s) => (
              <CopyBlock
                key={`${client}-${variant}-${shell}-${s.id}`}
                label={s.label}
                file={s.file}
                text={s.text}
                warning={s.warning}
                note={s.note}
                onCopied={() => onCopied(client, s.copyVariant)}
              />
            ))}
            <CopyBlock
              key={`${client}-agent`}
              label={agent.label}
              text={agent.text}
              warning={agent.warning}
              note={agent.note}
              onCopied={() => onCopied(client, agent.copyVariant)}
            />
            {info.docsUrl && (
              <p className="text-xs text-fg-muted">
                Checked against{' '}
                <a href={info.docsUrl} target="_blank" rel="noreferrer" className="text-accent hover:underline">
                  {info.label}'s MCP docs
                </a>
                .
              </p>
            )}
          </>
        )}
      </div>
    </SettingsCard>
  )
}
