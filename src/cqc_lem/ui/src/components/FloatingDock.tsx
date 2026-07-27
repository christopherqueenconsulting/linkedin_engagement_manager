import type { ReactNode } from 'react'

// ONE bottom-right stack for every floating control. Before this each of them was independently
// `fixed bottom-4 right-4 z-40`, so they simply overlapped and whichever came later in the DOM
// won — which is how the feedback widget ended up hiding Settings' Save All button (issue #596).
// The portal target is its OWN element rather than this container: React re-appends a host node it
// re-renders to the END of its parent, so a portaled bar sharing the container with `children`
// swaps places with the launcher the first time the feedback panel opens or closes. Two separate
// boxes make the stacking order structural instead of last-render-wins.
export const FLOATING_DOCK_ID = 'floating-dock'

export default function FloatingDock({ children }: { children?: ReactNode }) {
  return (
    <div
      // The container spans the tallest child, so its gaps would swallow clicks on the page
      // behind it — only the controls themselves take pointer events.
      className="fixed bottom-4 right-4 z-40 flex flex-col items-end gap-2 pointer-events-none [&>*]:pointer-events-auto"
    >
      {/* Portaled controls stack here, above `children`. `empty:hidden` so the slot adds no gap
          of its own while nothing is portaled in. */}
      <div id={FLOATING_DOCK_ID} className="flex flex-col items-end gap-2 empty:hidden" />
      {children}
    </div>
  )
}
