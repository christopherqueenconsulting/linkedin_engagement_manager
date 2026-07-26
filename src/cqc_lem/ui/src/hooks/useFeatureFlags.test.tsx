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
  // retry:false so an error case resolves in one tick instead of on the query's own back-off.
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
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
