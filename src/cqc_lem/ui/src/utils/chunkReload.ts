import { lazy, type ComponentType, type LazyExoticComponent } from 'react'

// Stale-chunk recovery (issue #743). Vite emits content-hashed lazy chunks and a tab holds their
// hashes in its in-memory module graph for as long as it stays open. Releases batch 4x daily, so a
// tab open across one loses every chunk it had not fetched yet — the next `import()` 404s and the
// feature reads as broken ("Failed to fetch dynamically imported module") rather than as "reload me".
// The server-side archive (cqc_lem.api.spa_assets) keeps the last few builds' assets resolvable and
// is the half that costs the user nothing; this is the fallback for anything older than the archive.
//
// index.html is `no-store`, so ONE reload always lands on the current build. The guard is what makes
// that safe: a marker in sessionStorage means a second failure inside the cooldown surfaces a message
// instead of reloading again. A tab that cannot PERSIST the marker never reloads at all — with no way
// to remember an attempt there is no way to stop a loop, and a wrong message beats a reload loop.

const RELOAD_MARKER = 'lem:chunk-reload-at'
const RELOAD_COOLDOWN_MS = 60_000

export const CHUNK_RELOAD_BLOCKED_EVENT = 'lem:chunk-reload-blocked'
export const NEW_VERSION_MESSAGE = 'A new version was released — please refresh.'
export const RELOADING_MESSAGE = 'A new version was released — reloading…'

// Matched against the error MESSAGE because no browser gives this a stable error type: Chrome says
// "Failed to fetch dynamically imported module", Safari "Importing a module script failed", and
// bundlers name it ChunkLoadError.
const CHUNK_ERROR_PATTERNS = [
  'failed to fetch dynamically imported module',
  'error loading dynamically imported module',
  'failed to load module script',
  'importing a module script failed',
  'unable to preload css',
  'chunkloaderror',
]

export type ChunkRecovery = 'reloaded' | 'blocked' | 'ignored'

let lastAttemptAt: number | null = null
let armed = false

function messageOf(reason: unknown): string {
  if (typeof reason === 'string') return reason
  if (reason instanceof Error) return `${reason.name}: ${reason.message}`
  const named = reason as { name?: unknown; message?: unknown } | null
  if (named && (typeof named.message === 'string' || typeof named.name === 'string')) {
    return `${typeof named.name === 'string' ? named.name : ''}: ${typeof named.message === 'string' ? named.message : ''}`
  }
  return ''
}

export function isChunkLoadError(reason: unknown): boolean {
  const message = messageOf(reason).toLowerCase()
  if (!message) return false
  return CHUNK_ERROR_PATTERNS.some((pattern) => message.includes(pattern))
}

function readMarker(): number | null {
  try {
    const raw = window.sessionStorage.getItem(RELOAD_MARKER)
    if (raw === null) return null
    const at = Number(raw)
    return Number.isFinite(at) ? at : null
  } catch {
    return null
  }
}

function writeMarker(now: number): boolean {
  try {
    window.sessionStorage.setItem(RELOAD_MARKER, String(now))
    return true
  } catch {
    return false
  }
}

// A dynamic import that failed because the tab is OFFLINE reports the same message a stale chunk
// does ("Failed to fetch dynamically imported module"), and `vite:preloadError` does not report a
// reason at all. index.html is `no-store`, so reloading an offline tab cannot re-fetch the shell —
// it replaces a working app with the browser's own offline page. `navigator.onLine` is only
// trustworthy when it says false, which is exactly the direction this needs.
function isOffline(): boolean {
  return typeof navigator !== 'undefined' && navigator.onLine === false
}

function announceBlocked(): ChunkRecovery {
  try {
    window.dispatchEvent(new CustomEvent(CHUNK_RELOAD_BLOCKED_EVENT))
  } catch {
    // A window without CustomEvent still gets the thrown NEW_VERSION_MESSAGE from the caller.
  }
  return 'blocked'
}

/**
 * Recover from a failed chunk load by reloading once. `force` is for signals that ARE a chunk
 * failure by definition (Vite's `vite:preloadError`), whose payload message may not match.
 */
export function recoverFromChunkError(
  reason: unknown,
  options: { force?: boolean; now?: number } = {},
): ChunkRecovery {
  if (!options.force && !isChunkLoadError(reason)) return 'ignored'
  if (typeof window === 'undefined') return 'ignored'
  // Not a new version — leave the original failure to the caller's own error UI, which is
  // recoverable once the connection comes back. A reload here would not be.
  if (isOffline()) return 'ignored'

  const now = options.now ?? Date.now()
  const previous = lastAttemptAt ?? readMarker()
  if (previous !== null && now - previous < RELOAD_COOLDOWN_MS) return announceBlocked()
  if (!writeMarker(now)) return announceBlocked()

  lastAttemptAt = now
  window.location.reload()
  return 'reloaded'
}

/**
 * Wrap a dynamic import so a stale chunk self-heals. The rejection is replaced with a plain-language
 * error: the caller's own error UI (a mutation's onError, an error boundary) then says why rather
 * than surfacing the browser's module-loader wording.
 */
export async function importWithChunkRecovery<T>(load: () => Promise<T>): Promise<T> {
  try {
    return await load()
  } catch (err) {
    const outcome = recoverFromChunkError(err)
    if (outcome === 'ignored') throw err
    throw new Error(outcome === 'reloaded' ? RELOADING_MESSAGE : NEW_VERSION_MESSAGE, { cause: err })
  }
}

/**
 * `React.lazy` with the same recovery, so a route chunk lost to a deploy self-heals instead of
 * bubbling a module-loader error into the nearest boundary.
 */
export function lazyWithChunkRecovery<
  // Mirrors React's own `lazy` signature — a narrower constraint rejects components with props.
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  T extends ComponentType<any>,
>(load: () => Promise<{ default: T }>): LazyExoticComponent<T> {
  return lazy(() => importWithChunkRecovery(load))
}

/** Wire the window-level signals. Idempotent — a second call re-uses the first arming. */
export function initChunkReload(): void {
  if (typeof window === 'undefined' || armed) return
  armed = true

  // Vite's own preload-failure hook. Its default action rethrows, which surfaces as a dead screen,
  // so preventDefault() once we have taken the failure over.
  window.addEventListener('vite:preloadError', (event: Event) => {
    const payload = (event as Event & { payload?: unknown }).payload
    if (recoverFromChunkError(payload ?? event, { force: true }) !== 'ignored') {
      event.preventDefault()
    }
  })

  // A dynamic import nobody caught (an un-awaited route/feature load).
  window.addEventListener('unhandledrejection', (event: PromiseRejectionEvent) => {
    if (recoverFromChunkError(event.reason) === 'reloaded') event.preventDefault()
  })
}

/** Test seam — clears the in-process half of the guard. */
export function resetChunkReloadState(): void {
  lastAttemptAt = null
  armed = false
}
