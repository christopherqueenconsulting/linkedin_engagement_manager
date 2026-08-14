import { createContext, useContext, useEffect, useRef } from 'react'
import { EVENTS, capture } from '../../utils/analytics'

// One shared registry so every settings section exposes the SAME save it already uses (single
// source of truth). "Save All" saves only the DIRTY sections (never clobbering untouched ones),
// and the unsaved-changes guard fires whenever any section is dirty.
// The context object and the hooks around it live here, apart from the provider and the Save All
// bar, so those component files export components only (Fast Refresh).
export type SaveSection = {
  label: string
  isDirty: boolean
  save: () => Promise<boolean> // resolves false / throws on failure
}

export type SettingsSaveCtxValue = {
  register: (id: string, section: SaveSection) => void
  unregister: (id: string) => void
  dirtyIds: string[]
  savingAll: boolean
  summary: { label: string; ok: boolean }[] | null
  saveAll: () => Promise<void>
}

export const SettingsSaveCtx = createContext<SettingsSaveCtxValue | null>(null)

export function useSettingsSave(): SettingsSaveCtxValue | null {
  return useContext(SettingsSaveCtx)
}

// Cards call this to plug into Save All / the unsaved guard. No-op when rendered outside a
// provider, so cards stay usable on their own. `save` is kept in a ref so its closure can change
// each render without churning the registration effect — the ref is written after commit, and the
// registered wrapper only reads it when a save actually runs.
export function useRegisterSaveSection(
  id: string, label: string, isDirty: boolean, save: () => Promise<boolean>
) {
  const ctx = useSettingsSave()
  const saveRef = useRef(save)
  useEffect(() => { saveRef.current = save })
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
