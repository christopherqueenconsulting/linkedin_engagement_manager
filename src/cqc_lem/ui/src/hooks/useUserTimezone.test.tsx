import { describe, expect, it, vi, afterEach, beforeEach } from 'vitest'
import type { ReactNode } from 'react'
import { cleanup, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { useUserTimezone, useUserTimezoneState } from './useUserTimezone'

const get = vi.fn()
vi.mock('../api/client', () => ({ default: { get: (...args: unknown[]) => get(...args) } }))

let sessionToken: string | null = 'tok'
vi.mock('../contexts/useAuth', () => ({
  useAuth: () => ({ sessionToken }),
}))

// What the hook falls back to when the user's own zone isn't known yet. Read rather than mocked so
// the assertions hold on any machine — the point is that the fallback is never reported as resolved.
const browserZone = Intl.DateTimeFormat().resolvedOptions().timeZone

// retryDelay:0, NOT retry:false — the hook sets `retry: 3` on the query itself so a blip can't lock
// the pickers, and a per-query option beats a client default. Only the back-off is a default.
function harness(ui: ReactNode) {
  const client = new QueryClient({ defaultOptions: { queries: { retryDelay: 0 } } })
  return render(<QueryClientProvider client={client}>{ui}</QueryClientProvider>)
}

function Probe() {
  const { timezone, isResolved } = useUserTimezoneState()
  return <span data-testid="value">{`${timezone}|${String(isResolved)}`}</span>
}

function StringProbe() {
  return <span data-testid="value">{useUserTimezone()}</span>
}

beforeEach(() => {
  get.mockReset()
  sessionToken = 'tok'
})
afterEach(cleanup)

describe('useUserTimezoneState (issue #774)', () => {
  it('is unresolved while the request is in flight, and the zone is the browser guess', () => {
    get.mockReturnValue(new Promise(() => {})) // never resolves
    harness(<Probe />)
    expect(screen.getByTestId('value').textContent).toBe(`${browserZone}|false`)
  })

  it('resolves to the user-configured zone once /user/timezone answers', async () => {
    get.mockResolvedValue({ data: { detail: { timezone: 'America/New_York' } } })
    harness(<Probe />)
    await waitFor(() =>
      expect(screen.getByTestId('value').textContent).toBe('America/New_York|true'),
    )
  })

  it('stays unresolved when the request fails — a guess is never reported as the setting', async () => {
    get.mockRejectedValue(new Error('boom'))
    harness(<Probe />)
    // 1 attempt + 3 retries, then it gives up — and still reports unresolved.
    await waitFor(() => expect(get).toHaveBeenCalledTimes(4))
    expect(screen.getByTestId('value').textContent).toBe(`${browserZone}|false`)
  })

  it('stays unresolved with no session token — the query never runs', () => {
    sessionToken = null
    harness(<Probe />)
    expect(get).not.toHaveBeenCalled()
    expect(screen.getByTestId('value').textContent).toBe(`${browserZone}|false`)
  })

  it('useUserTimezone keeps returning a bare string for display-only callers', async () => {
    get.mockResolvedValue({ data: { detail: { timezone: 'Europe/Berlin' } } })
    harness(<StringProbe />)
    await waitFor(() => expect(screen.getByTestId('value').textContent).toBe('Europe/Berlin'))
  })
})
