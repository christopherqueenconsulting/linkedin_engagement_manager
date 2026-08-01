import { useEffect, useState } from 'react'
import api from '../api/client'

// New-version awareness (issue #754, phase 2 of #743).
//
// #743's two layers are REACTIVE — asset retention and the one silent reload both fire only after a
// lazy chunk has already failed to load. This is the proactive half: releases batch 4x daily, so a
// long-lived tab is routinely several builds behind, and it can keep WORKING while running old
// client code against a newer API. Polling `/api/app-info` turns that into an expected prompt.
//
// This half NEVER reloads on its own. The one automatic reload stays owned by `chunkReload.ts`; all
// this does is flip `NewVersionNotice` on and leave the decision to the user, so the two signals can
// never both act on the same tab.

export const VERSION_POLL_INTERVAL_MS = 5 * 60 * 1000

// The version the bundle booted with — the first one this tab ever read. Module scope rather than
// state, so remounting the notice can never re-baseline against a build that shipped after boot.
let bootVersion: string | null = null

function versionOf(payload: unknown): string {
  const detail = (payload as { detail?: { version?: unknown } } | null)?.detail
  return typeof detail?.version === 'string' ? detail.version.trim() : ''
}

/**
 * One poll. Resolves true only when the server reports a version DIFFERENT from the one this tab
 * booted with. An unreachable endpoint or a missing/blank version resolves false and does not
 * baseline — an unknown version is never "new", in either direction.
 */
export async function checkForNewVersion(): Promise<boolean> {
  let version: string
  try {
    version = versionOf((await api.get('/app-info')).data)
  } catch {
    return false
  }
  if (!version) return false
  if (bootVersion === null) {
    bootVersion = version
    return false
  }
  return version !== bootVersion
}

/** True once the running release differs from the one this tab loaded. Never goes back to false. */
export function useNewVersion(intervalMs: number = VERSION_POLL_INTERVAL_MS): boolean {
  const [isStale, setIsStale] = useState(false)

  useEffect(() => {
    // Detected once is enough: the prompt is already up, so the interval is torn down rather than
    // left running behind it.
    if (isStale) return
    let cancelled = false

    // `force` is the boot read only. A tab opened in the BACKGROUND (ctrl-click, a restored
    // session) still loads and runs this bundle while hidden, so skipping its first read would
    // baseline it against whatever shipped by the time the user finally looked at it — the tab
    // would be running old code and could never be told. Every later poll respects visibility.
    const poll = async (force = false) => {
      if (!force && document.visibilityState === 'hidden') return
      const isNew = await checkForNewVersion()
      if (isNew && !cancelled) setIsStale(true)
    }

    void poll(true) // the first run is what records the boot baseline
    const timer = window.setInterval(() => void poll(), intervalMs)
    const onVisibilityChange = () => void poll()
    document.addEventListener('visibilitychange', onVisibilityChange)

    return () => {
      cancelled = true
      window.clearInterval(timer)
      document.removeEventListener('visibilitychange', onVisibilityChange)
    }
  }, [intervalMs, isStale])

  return isStale
}

/** Test seam — forgets the boot baseline. */
export function resetVersionWatchState(): void {
  bootVersion = null
}
