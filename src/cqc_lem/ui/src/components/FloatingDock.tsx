import type { ReactNode } from 'react'

// ONE bottom-right stack for every floating control. Before this each of them was independently
// `fixed bottom-4 right-4 z-40`, so they simply overlapped and whichever came later in the DOM
// won — which is how the feedback widget ended up hiding Settings' Save All button (issue #596).
// Column-REVERSE so the first child (the always-present feedback launcher) stays anchored at the
// bottom and anything portaled in later stacks ABOVE it rather than on top of it.
export const FLOATING_DOCK_ID = 'floating-dock'

export default function FloatingDock({ children }: { children?: ReactNode }) {
  return (
    <div
      id={FLOATING_DOCK_ID}
      // The container spans the tallest child, so its gaps would swallow clicks on the page
      // behind it — only the controls themselves take pointer events.
      className="fixed bottom-4 right-4 z-40 flex flex-col-reverse items-end gap-2 pointer-events-none [&>*]:pointer-events-auto"
    >
      {children}
    </div>
  )
}
