import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, render, screen } from '@testing-library/react'
import NewVersionNotice from '../components/NewVersionNotice'
import {
  VERSION_POLL_INTERVAL_MS,
  checkForNewVersion,
  resetVersionWatchState,
  useNewVersion,
} from './useNewVersion'
import { NEW_VERSION_MESSAGE } from '../utils/chunkReload'

const get = vi.fn()
vi.mock('../api/client', () => ({ default: { get: (...args: unknown[]) => get(...args) } }))

function appInfo(version: unknown) {
  return { data: { detail: { version, show_version: true } } }
}

let reload: ReturnType<typeof vi.fn>

beforeEach(() => {
  vi.useFakeTimers()
  resetVersionWatchState()
  get.mockReset()
  reload = vi.fn()
  Object.defineProperty(window, 'location', {
    configurable: true,
    value: { ...window.location, reload },
  })
})

afterEach(() => {
  cleanup()
  restoreVisibility()
  vi.useRealTimers()
  vi.restoreAllMocks()
})

// jsdom defines `visibilityState` on Document.prototype; an own property shadows it.
function setHidden(hidden: boolean): void {
  Object.defineProperty(document, 'visibilityState', {
    configurable: true,
    get: () => (hidden ? 'hidden' : 'visible'),
  })
}

function restoreVisibility(): void {
  delete (document as unknown as Record<string, unknown>).visibilityState
}

function Probe({ intervalMs }: { intervalMs?: number }) {
  return <span data-testid="value">{String(useNewVersion(intervalMs))}</span>
}

function value(): string {
  return screen.getByTestId('value').textContent ?? ''
}

/** Run the mount poll (and any timers it queued) inside act, so state lands before assertions. */
async function settle(ms = 0): Promise<void> {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms)
  })
}

describe('checkForNewVersion (issue #754)', () => {
  it('baselines on the first read and only then reports a difference', async () => {
    get.mockResolvedValueOnce(appInfo('0.120.0'))
    expect(await checkForNewVersion()).toBe(false) // this IS the boot version

    get.mockResolvedValueOnce(appInfo('0.120.0'))
    expect(await checkForNewVersion()).toBe(false)

    get.mockResolvedValueOnce(appInfo('0.121.0'))
    expect(await checkForNewVersion()).toBe(true)
  })

  it('is quiet when the endpoint is unreachable — and does not baseline off the failure', async () => {
    get.mockRejectedValueOnce(new Error('Network Error'))
    expect(await checkForNewVersion()).toBe(false)

    get.mockResolvedValueOnce(appInfo('0.120.0'))
    expect(await checkForNewVersion()).toBe(false) // first version READ is still the baseline

    get.mockResolvedValueOnce(appInfo('0.121.0'))
    expect(await checkForNewVersion()).toBe(true)
  })

  it('treats a missing, blank or non-string version as unknown, never as new', async () => {
    get.mockResolvedValueOnce(appInfo('0.120.0'))
    await checkForNewVersion()

    for (const bad of [undefined, '', '   ', 42, null]) {
      get.mockResolvedValueOnce(appInfo(bad))
      expect(await checkForNewVersion()).toBe(false)
    }
    get.mockResolvedValueOnce({ data: {} })
    expect(await checkForNewVersion()).toBe(false)
  })
})

describe('useNewVersion polling', () => {
  it('surfaces a version change on the next poll, with nothing having failed to load', async () => {
    get.mockResolvedValue(appInfo('0.120.0'))
    render(<Probe />)
    await settle()
    expect(value()).toBe('false')

    get.mockResolvedValue(appInfo('0.121.0'))
    await settle(VERSION_POLL_INTERVAL_MS)
    expect(value()).toBe('true')
  })

  it('stays quiet forever on an identical version', async () => {
    get.mockResolvedValue(appInfo('0.120.0'))
    render(<Probe />)
    await settle(VERSION_POLL_INTERVAL_MS * 5)
    expect(value()).toBe('false')
  })

  it('stays quiet when every poll fails', async () => {
    get.mockRejectedValue(new Error('Network Error'))
    render(<Probe />)
    await settle(VERSION_POLL_INTERVAL_MS * 3)
    expect(value()).toBe('false')
  })

  it('does not poll while the tab is hidden', async () => {
    get.mockResolvedValue(appInfo('0.120.0'))
    render(<Probe />)
    await settle()
    expect(get).toHaveBeenCalledTimes(1)

    setHidden(true)
    await settle(VERSION_POLL_INTERVAL_MS * 3)
    expect(get).toHaveBeenCalledTimes(1)
  })

  it('still baselines at boot in a tab that opened in the background', async () => {
    // A ctrl-clicked tab runs this bundle while hidden. Skipping its boot read would baseline it
    // against whatever shipped before the user first looked at it, and the prompt would never come.
    setHidden(true)
    get.mockResolvedValue(appInfo('0.120.0'))
    render(<Probe />)
    await settle()
    expect(get).toHaveBeenCalledTimes(1)

    get.mockResolvedValue(appInfo('0.121.0'))
    await settle(VERSION_POLL_INTERVAL_MS * 3)
    expect(get).toHaveBeenCalledTimes(1) // hidden: no polling after the boot read
    expect(value()).toBe('false')

    setHidden(false)
    await act(async () => {
      document.dispatchEvent(new Event('visibilitychange'))
      await vi.advanceTimersByTimeAsync(0)
    })
    expect(value()).toBe('true')
  })

  it('catches up as soon as the tab comes back to the foreground', async () => {
    get.mockResolvedValue(appInfo('0.120.0'))
    setHidden(false)
    render(<Probe />)
    await settle()

    setHidden(true)
    get.mockResolvedValue(appInfo('0.121.0'))
    await settle(VERSION_POLL_INTERVAL_MS)
    expect(value()).toBe('false')

    setHidden(false)
    await act(async () => {
      document.dispatchEvent(new Event('visibilitychange'))
      await vi.advanceTimersByTimeAsync(0)
    })
    expect(value()).toBe('true')
  })

  it('stops polling once the prompt is up — the answer cannot change', async () => {
    get.mockResolvedValue(appInfo('0.120.0'))
    render(<Probe />)
    await settle()
    get.mockResolvedValue(appInfo('0.121.0'))
    await settle(VERSION_POLL_INTERVAL_MS)
    expect(value()).toBe('true')

    const calls = get.mock.calls.length
    await settle(VERSION_POLL_INTERVAL_MS * 3)
    expect(get).toHaveBeenCalledTimes(calls)
  })

  it('clears its interval on unmount', async () => {
    get.mockResolvedValue(appInfo('0.120.0'))
    const { unmount } = render(<Probe />)
    await settle()
    unmount()
    await settle(VERSION_POLL_INTERVAL_MS * 3)
    expect(get).toHaveBeenCalledTimes(1)
  })
})

describe('NewVersionNotice — proactive prompt', () => {
  it('renders nothing while the tab is on the current release', async () => {
    get.mockResolvedValue(appInfo('0.120.0'))
    const { container } = render(<NewVersionNotice />)
    await settle(VERSION_POLL_INTERVAL_MS)
    expect(container.firstChild).toBeNull()
  })

  it('asks for a refresh — and never reloads by itself', async () => {
    get.mockResolvedValue(appInfo('0.120.0'))
    render(<NewVersionNotice />)
    await settle()

    get.mockResolvedValue(appInfo('0.121.0'))
    await settle(VERSION_POLL_INTERVAL_MS)

    expect(screen.getByRole('alert')).toBeTruthy()
    expect(screen.getByText(NEW_VERSION_MESSAGE)).toBeTruthy()
    expect(screen.getByText(/refreshing picks up the latest version/i)).toBeTruthy()
    expect(reload).not.toHaveBeenCalled()

    screen.getByRole('button', { name: /refresh now/i }).click()
    expect(reload).toHaveBeenCalledTimes(1)
  })
})
