import { describe, expect, it, vi, afterEach, beforeEach } from 'vitest'
import type { ReactNode } from 'react'
import { cleanup, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { FLAGS, useFeatureFlag, useFeatureFlags } from './useFeatureFlags'

const get = vi.fn()
vi.mock('../api/client', () => ({ default: { get: (...args: unknown[]) => get(...args) } }))

function payload(flags: Record<string, boolean>, localEvaluation = true) {
  return { data: { detail: { distinct_id: 'system', flags, local_evaluation: localEvaluation } } }
}

function harness(ui: ReactNode) {
  // retryDelay:0, NOT retry:false — useFeatureFlags() sets `retry: 1` on the query itself (one
  // transient blip must not drop the whole bootstrap), and a per-query option beats a client
  // default, so the retry happens either way. Only the back-off is a default, and left at the
  // real one (~1s) the error case settles slower than waitFor's 1s timeout.
  const client = new QueryClient({ defaultOptions: { queries: { retryDelay: 0 } } })
  return render(<QueryClientProvider client={client}>{ui}</QueryClientProvider>)
}

function Probe({ fallback }: { fallback?: boolean }) {
  const enabled = useFeatureFlag(FLAGS.tutorialVideos, fallback)
  const { isLoading } = useFeatureFlags()
  return <span data-testid="value">{isLoading ? 'loading' : String(enabled)}</span>
}

beforeEach(() => {
  get.mockReset()
  localStorage.clear()
})
afterEach(cleanup)

describe('useFeatureFlag (issue #651)', () => {
  it('renders the fallback until the bootstrap lands, then the server value', async () => {
    get.mockReturnValue(new Promise(() => {})) // never resolves
    harness(<Probe />)
    expect(screen.getByTestId('value').textContent).toBe('loading')

    cleanup()
    get.mockResolvedValue(payload({ [FLAGS.tutorialVideos]: true }))
    harness(<Probe />)
    await waitFor(() => expect(screen.getByTestId('value').textContent).toBe('true'))
  })

  it('falls back to the SAFE value when the API is unreachable — never turns a feature on', async () => {
    get.mockRejectedValue(new Error('network down'))
    harness(<Probe />)
    await waitFor(() => expect(screen.getByTestId('value').textContent).toBe('false'))
  })

  it('honours an explicit fallback for a flag missing from the payload', async () => {
    get.mockResolvedValue(payload({ 'some-other-flag': true }))
    harness(<Probe fallback />)
    await waitFor(() => expect(screen.getByTestId('value').textContent).toBe('true'))
  })

  it('reports a server false even though the flag key IS present', async () => {
    get.mockResolvedValue(payload({ [FLAGS.tutorialVideos]: false }))
    harness(<Probe fallback />)
    await waitFor(() => expect(screen.getByTestId('value').textContent).toBe('false'))
  })

  it('sends the session token so a per-user rollout can reach this browser', async () => {
    localStorage.setItem('lem_session', 'tok en')
    get.mockResolvedValue(payload({}))
    harness(<Probe />)
    await waitFor(() => expect(get).toHaveBeenCalled())
    expect(get).toHaveBeenCalledWith('/flags?session_token=tok%20en')
  })

  it('asks anonymously when there is no session — the landing page is logged out', async () => {
    get.mockResolvedValue(payload({}))
    harness(<Probe />)
    await waitFor(() => expect(get).toHaveBeenCalled())
    expect(get).toHaveBeenCalledWith('/flags')
  })
})
