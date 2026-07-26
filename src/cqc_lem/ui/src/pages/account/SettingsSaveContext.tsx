import {
  createContext, useCallback, useContext, useEffect, useMemo, useRef, useState,
  type ReactNode,
} from 'react'
import { EVENTS, capture } from '../../utils/analytics'

// One shared registry so every settings section exposes the SAME save it already uses (single
// source of truth). "Save All" saves only the DIRTY sections (never clobbering untouched ones),
// and the unsaved-changes guard fires whenever any section is dirty.
export type SaveSection = {
  label: string
  isDirty: boolean
  save: () => Promise<boolean> // resolves false / throws on failure
}

type Ctx = {
  register: (id: string, section: SaveSection) => void
  unregister: (id: string) => void
  dirtyIds: string[]
  savingAll: boolean
  summary: { label: string; ok: boolean }[] | null
  saveAll: () => Promise<void>
}

const SettingsSaveCtx = createContext<Ctx | null>(null)

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

  const value = useMemo<Ctx>(
    () => ({ register, unregister, dirtyIds, savingAll, summary, saveAll }),
    [register, unregister, dirtyIds, savingAll, summary, saveAll]
  )
  return <SettingsSaveCtx.Provider value={value}>{children}</SettingsSaveCtx.Provider>
}

function useSettingsSave(): Ctx | null {
  return useContext(SettingsSaveCtx)
}

// Cards call this to plug into Save All / the unsaved guard. No-op when rendered outside a
// provider, so cards stay usable on their own. `save` is kept in a ref so its closure can change
// each render without churning the registration effect.
export function useRegisterSaveSection(
  id: string, label: string, isDirty: boolean, save: () => Promise<boolean>
) {
  const ctx = useSettingsSave()
  const saveRef = useRef(save)
  saveRef.current = save
  const register = ctx?.register
  const unregister = ctx?.unregister
  useEffect(() => {
    if (!register || !unregister) return
    register(id, { label, isDirty, save: () => saveRef.current() })
    return () => unregister(id)
  }, [id, label, isDirty, register, unregister])
}

// Most cards ALSO keep their own Save button, which calls their mutation directly and never goes
// through saveAll — so without this, prefs_saved would only ever count the users who happen to use
// Save All and every rate built on it would be wrong. Spread onto the card's own mutate() call:
//   onClick={() => groupsMutation.mutate(undefined, sectionSaveCallbacks('groups'))}
// `section` must be the SAME id the card registers, or the two doors report different sections.
export function sectionSaveCallbacks(section: string): { onSuccess: () => void; onError: () => void } {
  return {
    onSuccess: () => capture(EVENTS.prefsSaved, { section, ok: true }),
    onError: () => capture(EVENTS.prefsSaved, { section, ok: false }),
  }
}

// beforeunload (tab close / refresh) + capture-phase interception of in-app <a> navigation.
// BrowserRouter (not a data router) can't use useBlocker, so we intercept link clicks before
// React Router handles them and confirm before allowing a route change away from this page.
export function useUnsavedChangesGuard(enabled: boolean) {
  useEffect(() => {
    if (!enabled) return
    const onBeforeUnload = (e: BeforeUnloadEvent) => { e.preventDefault(); e.returnValue = '' }
    const onClick = (e: MouseEvent) => {
      if (e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return
      const anchor = (e.target as HTMLElement)?.closest?.('a')
      if (!anchor) return
      const href = anchor.getAttribute('href')
      if (!href || anchor.target === '_blank' || href.startsWith('#') || href.startsWith('http')) return
      const url = new URL(href, window.location.origin)
      if (url.pathname === window.location.pathname) return // same page (e.g. ?tab= switch) — allow
      if (!window.confirm('You have unsaved changes. Leave this page and discard them?')) {
        e.preventDefault()
        e.stopPropagation()
      }
    }
    window.addEventListener('beforeunload', onBeforeUnload)
    document.addEventListener('click', onClick, true)
    return () => {
      window.removeEventListener('beforeunload', onBeforeUnload)
      document.removeEventListener('click', onClick, true)
    }
  }, [enabled])
}

// Fixed bottom-right Save All control + per-section result summary. Also wires the unsaved guard.
export function SaveAllBar() {
  const ctx = useSettingsSave()
  useUnsavedChangesGuard((ctx?.dirtyIds.length ?? 0) > 0)
  if (!ctx) return null
  const { dirtyIds, savingAll, summary, saveAll } = ctx
  if (dirtyIds.length === 0 && !summary && !savingAll) return null
  return (
    <div className="fixed bottom-4 right-4 z-40 max-w-xs">
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
}
