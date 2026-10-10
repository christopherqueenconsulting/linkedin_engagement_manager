import { useAppInfo } from '../hooks/useAppInfo'

export const DEMO_BADGE_TEST_ID = 'demo-data-badge'

// "Demo data" corner badge (issue #2372). Renders ONLY when the server reports DEMO_MODE on via
// /api/app-info — absent, false or still loading all render nothing. Pinned to the bottom-left
// corner, clear of the floating dock (bottom-right) and the nav, so a recording can crop it out
// with one rectangle. pointer-events-none keeps it from ever covering a control.
export default function DemoBadge() {
  const { data } = useAppInfo()
  if (data?.demo_mode !== true) return null
  return (
    <div
      data-testid={DEMO_BADGE_TEST_ID}
      role="status"
      className="fixed bottom-3 left-3 z-[60] pointer-events-none select-none rounded-md bg-amber-500/90 px-2 py-1 text-[11px] font-semibold uppercase tracking-wide text-white shadow"
    >
      Demo data
    </div>
  )
}
