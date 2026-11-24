import { forwardRef } from 'react'
import CopyBlock from '../../components/CopyBlock'

/**
 * The show-once panel (issue #2687). Rendered whenever a freshly minted token is in component
 * state — on its own, NOT inside the connect commands — so a token is never minted and then
 * hidden because some other part of the page (the URL, a client tab) could not render.
 */
const ShownTokenPanel = forwardRef<HTMLHeadingElement, {
  token: string
  onCopied: () => void
  onHide: () => void
}>(function ShownTokenPanel({ token, onCopied, onHide }, heading) {
  return (
    <div className="rounded-panel border border-warning/50 bg-warning/5 p-4 space-y-3">
      <h2 ref={heading} tabIndex={-1} className="text-sm font-semibold text-warning focus:outline-none">
        Shown once — copy what you need now
      </h2>
      <p className="text-xs text-warning">
        Your new token is below and filled into the connect commands. When you leave this page or hide
        it, it is gone for good; rotating the connection is the only way to get another.
      </p>
      <CopyBlock
        label="Your token"
        text={token}
        warning="Paste only where a client asks for it — never into your agent's chat"
        onCopied={onCopied}
      />
      <button type="button" onClick={onHide} className="text-xs font-semibold text-warning hover:underline">
        I've copied it — hide the token
      </button>
    </div>
  )
})

export default ShownTokenPanel
