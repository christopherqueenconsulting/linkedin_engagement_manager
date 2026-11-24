import { forwardRef, useState } from 'react'
import CopyBlock from '../../components/CopyBlock'
import SettingsCard from '../../components/SettingsCard'
import { CLIENTS, buildClientSnippets, clientInfo } from './clientSnippets'
import type { ClientId, SnippetVariant } from './clientSnippets'

const TAB_BASE = 'shrink-0 min-h-9 px-3 py-1.5 rounded-control text-xs font-semibold whitespace-nowrap border'

/**
 * The per-client connect commands (issue #2687, plan §6 card 2).
 *
 * `token` is non-null ONLY inside the show-once window — the page holds it in component state and
 * nowhere else — and every block falls back to `<YOUR_TOKEN>` the moment it is cleared. The
 * primary variant puts the token on one `export` line and references the variable everywhere
 * after it; the inline variant is behind a deliberate toggle because it writes the secret into a
 * config file or a command line.
 */
const ConnectCommands = forwardRef<HTMLHeadingElement, {
  url: string
  token: string | null
  client: ClientId
  onClientChange: (client: ClientId) => void
  onCopied: (client: ClientId, variant: string) => void
  onHideToken: () => void
}>(function ConnectCommands({ url, token, client, onClientChange, onCopied, onHideToken }, tokenHeading) {
  const [variant, setVariant] = useState<SnippetVariant>('env')
  const info = clientInfo(client)
  const snippets = info.supported ? buildClientSnippets(client, url, token, variant) : []
  const copiedVariant = token ? variant : 'placeholder'

  return (
    <SettingsCard
      title="Connect your agent"
      subtitle="Pick your client, then run its command in a terminal on the machine the agent runs on."
    >
      {token && (
        <div className="rounded-panel border border-warning/50 bg-warning/5 p-3 space-y-3">
          <h3 ref={tokenHeading} tabIndex={-1} className="text-sm font-semibold text-warning focus:outline-none">
            Shown once — copy what you need now
          </h3>
          <p className="text-xs text-warning">
            Your new token is filled into the commands below. When you leave this page or hide it, it
            is gone for good; rotating the connection is the only way to get another.
          </p>
          <CopyBlock
            label="Your token"
            text={token}
            warning="Paste only where a client asks for it — never into your agent's chat"
            onCopied={() => onCopied(client, 'token')}
          />
          <button
            type="button"
            onClick={onHideToken}
            className="text-xs font-semibold text-warning hover:underline"
          >
            I've copied it — hide the token
          </button>
        </div>
      )}

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
                shown once. Rotate a connection below to get a new paste-ready command.
              </p>
            )}
            <label className="flex items-start gap-2 text-xs text-fg">
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
            {snippets.map((s) => (
              <CopyBlock
                key={`${client}-${variant}-${s.id}`}
                label={s.label}
                file={s.file}
                text={s.text}
                warning={s.warning}
                note={s.note}
                onCopied={() => onCopied(client, copiedVariant)}
              />
            ))}
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
})

export default ConnectCommands
