import { useRef, useState } from 'react'
import { maskProps } from '../utils/analytics'

const BUTTON_CLASS =
  'shrink-0 min-h-9 bg-raised border border-field text-fg px-3 py-1.5 rounded-control text-xs font-semibold ' +
  'hover:bg-page'
const PRE_CLASS =
  'm-0 px-3 py-2 text-xs font-mono text-fg whitespace-pre-wrap break-all bg-raised max-h-80 overflow-auto'

/**
 * A copyable block of text — a terminal command, a config file, a prompt (issue #2687).
 *
 * Masked by DEFAULT (`maskProps`): what goes in one of these is usually a credential or a command
 * built around one, and the safe failure for a new call site that forgets the prop is "masked in
 * replay and out of autocapture", not "recorded". The copy result is the same ok/failed tri-state
 * EngagementSuggestionsCard uses: a browser that refuses the clipboard is told so, and the text is
 * selected for a hand copy instead of the button quietly doing nothing.
 */
export default function CopyBlock({
  text,
  label,
  file,
  warning,
  note,
  mask = true,
  onCopied,
}: {
  text: string
  label: string
  file?: string
  warning?: string
  note?: string
  mask?: boolean
  /** Called once per successful copy — the analytics hook. Never passed the text. */
  onCopied?: () => void
}) {
  const [copied, setCopied] = useState<'ok' | 'failed' | null>(null)
  const pre = useRef<HTMLPreElement>(null)

  async function copy() {
    try {
      await navigator.clipboard.writeText(text)
      setCopied('ok')
      onCopied?.()
    } catch {
      setCopied('failed')
      const node = pre.current
      const selection = window.getSelection?.()
      if (node && selection) {
        const range = document.createRange()
        range.selectNodeContents(node)
        selection.removeAllRanges()
        selection.addRange(range)
      }
    }
    setTimeout(() => setCopied(null), 4000)
  }

  const masked = mask ? maskProps('') : { className: '' }

  return (
    <div {...masked} className={`${masked.className} rounded-panel border border-hairline overflow-hidden`.trim()}>
      <div className="flex items-center justify-between gap-3 bg-page px-3 py-2 border-b border-hairline">
        <div className="min-w-0">
          <p className="text-xs font-semibold text-fg truncate">
            {label}
            {file && <span className="ml-2 font-mono font-normal text-fg-muted">{file}</span>}
          </p>
          {warning && <p className="text-xs font-medium text-warning">{warning}</p>}
        </div>
        <button
          type="button"
          onClick={copy}
          aria-label={`${copied === 'ok' ? 'Copied' : 'Copy'} ${label}`}
          className={BUTTON_CLASS}
        >
          {copied === 'ok' ? 'Copied' : 'Copy'}
        </button>
      </div>
      <pre
        ref={pre}
        tabIndex={0}
        aria-label={label}
        className={PRE_CLASS}
      >
        {text}
      </pre>
      {note && <p className="px-3 pb-2 text-xs text-fg-muted">{note}</p>}
      <p role="status" className={copied === 'failed' ? 'px-3 pb-2 text-xs text-danger' : 'sr-only'}>
        {copied === 'ok' && 'Copied to clipboard.'}
        {copied === 'failed' && 'Could not copy. The text is selected — press Ctrl+C (or Cmd+C) to copy it.'}
      </p>
    </div>
  )
}
