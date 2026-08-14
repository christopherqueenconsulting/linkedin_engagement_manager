import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import { createPortal } from 'react-dom'
import { FLOATING_DOCK_ID } from '../../components/FloatingDock'
import { EVENTS, capture } from '../../utils/analytics'
import {
  SettingsSaveCtx, useSettingsSave, useUnsavedChangesGuard,
  type SaveSection, type SettingsSaveCtxValue,
} from './settingsSave'

export function SettingsSaveProvider({ children }: { children: ReactNode }) {
  const sections = useRef(new Map<string, SaveSection>())
  const [dirtyIds, setDirtyIds] = useState<string[]>([])
  const [savingAll, setSavingAll] = useState(false)
  const [summary, setSummary] = useState<{ label: string; ok: boolean }[] | null>(null)

  const recompute = useCallback(() => {
    const ids = [...sections.current.entries()].filter(([, s]) => s.isDirty).map(([id]) => id).sort()
    setDirtyIds((prev) => (prev.length === ids.length && prev.every((v, i) => v === ids[i]) ? prev : ids))
  }, [])

  const register = useCallback((id: string, section: SaveSection) => {
    sections.current.set(id, section)
    recompute()
  }, [recompute])

  const unregister = useCallback((id: string) => {
    sections.current.delete(id)
    recompute()
  }, [recompute])

  const saveAll = useCallback(async () => {
    const dirty = [...sections.current.entries()].filter(([, s]) => s.isDirty)
    if (dirty.length === 0) return
    setSavingAll(true)
    setSummary(null)
    const results: { label: string; ok: boolean }[] = []
    for (const [id, s] of dirty) {
      try {
        const ok = await s.save()
        results.push({ label: s.label, ok: ok !== false })
        capture(EVENTS.prefsSaved, { section: id, ok: ok !== false })
      } catch {
        results.push({ label: s.label, ok: false })
        capture(EVENTS.prefsSaved, { section: id, ok: false })
      }
    }
    setSavingAll(false)
    setSummary(results)
    recompute()
    setTimeout(() => setSummary(null), 6000)
  }, [recompute])

  const value = useMemo<SettingsSaveCtxValue>(
    () => ({ register, unregister, dirtyIds, savingAll, summary, saveAll }),
    [register, unregister, dirtyIds, savingAll, summary, saveAll]
  )
  return <SettingsSaveCtx.Provider value={value}>{children}</SettingsSaveCtx.Provider>
}

// Bottom-right Save All control + per-section result summary. Also wires the unsaved guard.
// It rides in Layout's FloatingDock rather than pinning itself to the same corner as the feedback
// widget, which used to render later in the DOM at the same z-index and bury it (issue #596).
export function SaveAllBar() {
  const ctx = useSettingsSave()
  // Layout commits the dock after this subtree renders, so it can only be read post-commit — the
  // one extra render is the cost. Falls back to self-positioning when used outside a Layout.
  const [dock, setDock] = useState<HTMLElement | null>(null)
  useUnsavedChangesGuard((ctx?.dirtyIds.length ?? 0) > 0)
  // eslint-disable-next-line react-hooks/set-state-in-effect
  useEffect(() => setDock(document.getElementById(FLOATING_DOCK_ID)), [])
  if (!ctx) return null
  const { dirtyIds, savingAll, summary, saveAll } = ctx
  if (dirtyIds.length === 0 && !summary && !savingAll) return null
  const bar = (
    <div className={dock ? 'max-w-xs' : 'fixed bottom-4 right-4 z-40 max-w-xs'}>
      {summary && (
        <div className="mb-2 bg-white rounded-lg shadow-lg border border-gray-200 p-3 space-y-1">
          {summary.map((r, i) => (
            <p key={i} className={`text-xs font-medium ${r.ok ? 'text-green-600' : 'text-red-600'}`}>
              {r.ok ? '✓' : '✕'} {r.label}
            </p>
          ))}
        </div>
      )}
      {(dirtyIds.length > 0 || savingAll) && (
        <button
          type="button"
          onClick={() => saveAll()}
          disabled={savingAll}
          className="w-full bg-blue-600 text-white px-5 py-3 rounded-full shadow-lg text-sm font-semibold hover:bg-blue-700 disabled:opacity-60 transition-colors"
        >
          {savingAll
            ? 'Saving all…'
            : `Save All${dirtyIds.length ? ` (${dirtyIds.length} unsaved)` : ''}`}
        </button>
      )}
    </div>
  )
  return dock ? createPortal(bar, dock) : bar
}
