import { Suspense } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, render, screen, waitFor } from '@testing-library/react'
import NewVersionNotice from '../components/NewVersionNotice'
import {
  CHUNK_RELOAD_BLOCKED_EVENT,
  NEW_VERSION_MESSAGE,
  RELOADING_MESSAGE,
  importWithChunkRecovery,
  initChunkReload,
  isChunkLoadError,
  lazyWithChunkRecovery,
  recoverFromChunkError,
  resetChunkReloadState,
} from './chunkReload'

const CHUNK_ERROR = new TypeError(
  'Failed to fetch dynamically imported module: https://lem.example.com/assets/jszip.min-BZWDyCXg.js',
)

let reload: ReturnType<typeof vi.fn>

beforeEach(() => {
  resetChunkReloadState()
  window.sessionStorage.clear()
  reload = vi.fn()
  Object.defineProperty(window, 'location', {
    configurable: true,
    value: { ...window.location, reload },
  })
})

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

describe('isChunkLoadError', () => {
  it('recognises the message every browser spells differently', () => {
    const messages = [
      'Failed to fetch dynamically imported module: /assets/x-abc.js',
      'error loading dynamically imported module',
      'Importing a module script failed.',
      'Unable to preload CSS for /assets/x-abc.css',
    ]
    messages.forEach((m) => expect(isChunkLoadError(new Error(m))).toBe(true))
    expect(isChunkLoadError({ name: 'ChunkLoadError', message: 'boom' })).toBe(true)
  })

  it('leaves ordinary failures alone', () => {
    expect(isChunkLoadError(new Error('Request failed with status code 500'))).toBe(false)
    expect(isChunkLoadError(undefined)).toBe(false)
    expect(isChunkLoadError(null)).toBe(false)
    expect(isChunkLoadError({})).toBe(false)
  })
})

describe('recoverFromChunkError', () => {
  it('reloads exactly once and then blocks — the loop guard', () => {
    expect(recoverFromChunkError(CHUNK_ERROR)).toBe('reloaded')
    expect(recoverFromChunkError(CHUNK_ERROR)).toBe('blocked')
    expect(recoverFromChunkError(CHUNK_ERROR)).toBe('blocked')
    expect(reload).toHaveBeenCalledTimes(1)
  })

  it('still blocks after the reload wiped the in-process guard (the marker survives)', () => {
    expect(recoverFromChunkError(CHUNK_ERROR, { now: 1_000 })).toBe('reloaded')
    resetChunkReloadState() // what a page reload does to module state
    expect(recoverFromChunkError(CHUNK_ERROR, { now: 5_000 })).toBe('blocked')
    expect(reload).toHaveBeenCalledTimes(1)
  })

  it('allows a fresh recovery once the cooldown has passed', () => {
    expect(recoverFromChunkError(CHUNK_ERROR, { now: 1_000 })).toBe('reloaded')
    resetChunkReloadState()
    expect(recoverFromChunkError(CHUNK_ERROR, { now: 1_000 + 60_001 })).toBe('reloaded')
    expect(reload).toHaveBeenCalledTimes(2)
  })

  it('never reloads when the marker cannot be persisted — no marker, no loop guard', () => {
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new Error('storage disabled')
    })
    expect(recoverFromChunkError(CHUNK_ERROR)).toBe('blocked')
    expect(reload).not.toHaveBeenCalled()
  })

  it('ignores an unrelated error', () => {
    expect(recoverFromChunkError(new Error('network down'))).toBe('ignored')
    expect(reload).not.toHaveBeenCalled()
  })

  it('force recovers a signal that IS a chunk failure regardless of its wording', () => {
    expect(recoverFromChunkError(new Error('opaque'), { force: true })).toBe('reloaded')
    expect(reload).toHaveBeenCalledTimes(1)
  })
})

describe('importWithChunkRecovery', () => {
  it('passes a successful import straight through', async () => {
    await expect(importWithChunkRecovery(async () => 'jszip')).resolves.toBe('jszip')
    expect(reload).not.toHaveBeenCalled()
  })

  it('reloads once and reports it in plain language', async () => {
    await expect(
      importWithChunkRecovery(() => Promise.reject(CHUNK_ERROR)),
    ).rejects.toThrow(RELOADING_MESSAGE)
    expect(reload).toHaveBeenCalledTimes(1)
  })

  it('asks the user to refresh once the guard blocks a second attempt', async () => {
    recoverFromChunkError(CHUNK_ERROR)
    await expect(
      importWithChunkRecovery(() => Promise.reject(CHUNK_ERROR)),
    ).rejects.toThrow(NEW_VERSION_MESSAGE)
    expect(reload).toHaveBeenCalledTimes(1)
  })

  it('rethrows a non-chunk failure untouched', async () => {
    const err = new Error('413 Payload Too Large')
    await expect(importWithChunkRecovery(() => Promise.reject(err))).rejects.toBe(err)
    expect(reload).not.toHaveBeenCalled()
  })
})

describe('lazyWithChunkRecovery', () => {
  it('renders normally when the route chunk loads', async () => {
    const Ok = lazyWithChunkRecovery(async () => ({ default: () => <p>route loaded</p> }))
    render(<Suspense fallback={<p>loading</p>}><Ok /></Suspense>)
    expect(await screen.findByText('route loaded')).toBeTruthy()
    expect(reload).not.toHaveBeenCalled()
  })

  it('recovers a route chunk lost to a deploy', async () => {
    const Stale = lazyWithChunkRecovery(() => Promise.reject(CHUNK_ERROR))
    // React logs the boundary-less rejection; the assertion is on the recovery, not the render.
    vi.spyOn(console, 'error').mockImplementation(() => {})
    render(<Suspense fallback={<p>loading</p>}><Stale /></Suspense>)
    await waitFor(() => expect(reload).toHaveBeenCalledTimes(1))
  })
})

describe('initChunkReload', () => {
  it("takes over Vite's preloadError and stops its default rethrow", () => {
    initChunkReload()
    const event = new Event('vite:preloadError', { cancelable: true })
    Object.assign(event, { payload: CHUNK_ERROR })
    window.dispatchEvent(event)
    expect(reload).toHaveBeenCalledTimes(1)
    expect(event.defaultPrevented).toBe(true)
  })

  it('recovers an uncaught dynamic-import rejection', () => {
    initChunkReload()
    const event = new Event('unhandledrejection', { cancelable: true })
    Object.assign(event, { reason: CHUNK_ERROR })
    window.dispatchEvent(event)
    expect(reload).toHaveBeenCalledTimes(1)
  })

  it('leaves unrelated rejections to the existing error handling', () => {
    initChunkReload()
    const event = new Event('unhandledrejection', { cancelable: true })
    Object.assign(event, { reason: new Error('api 500') })
    window.dispatchEvent(event)
    expect(reload).not.toHaveBeenCalled()
    expect(event.defaultPrevented).toBe(false)
  })

  it('arms the window listeners only once', () => {
    const add = vi.spyOn(window, 'addEventListener')
    initChunkReload()
    initChunkReload()
    expect(add).toHaveBeenCalledTimes(2) // preloadError + unhandledrejection, from the first call
  })
})

describe('NewVersionNotice', () => {
  it('renders nothing until a reload is blocked', () => {
    const { container } = render(<NewVersionNotice />)
    expect(container.firstChild).toBeNull()
  })

  it('asks for a refresh once the guard blocks', async () => {
    render(<NewVersionNotice />)
    act(() => {
      recoverFromChunkError(CHUNK_ERROR) // reloads
      recoverFromChunkError(CHUNK_ERROR) // blocked -> event
    })
    expect(await screen.findByRole('alert')).toBeTruthy()
    expect(screen.getByText(NEW_VERSION_MESSAGE)).toBeTruthy()
  })

  it('reloads on demand from the button', async () => {
    render(<NewVersionNotice />)
    act(() => {
      window.dispatchEvent(new CustomEvent(CHUNK_RELOAD_BLOCKED_EVENT))
    })
    const button = await screen.findByRole('button', { name: /refresh now/i })
    button.click()
    expect(reload).toHaveBeenCalledTimes(1)
  })
})
