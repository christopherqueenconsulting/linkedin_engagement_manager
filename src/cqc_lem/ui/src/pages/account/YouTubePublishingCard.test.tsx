import { describe, expect, it, vi, afterEach, beforeEach } from 'vitest'
import type { ReactNode } from 'react'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import YouTubePublishingCard from './YouTubePublishingCard'

const get = vi.fn()
vi.mock('../../api/client', () => ({ default: { get: (...args: unknown[]) => get(...args) } }))

let auth = { sessionToken: 'tok', isAdmin: true }
vi.mock('../../contexts/AuthContext', () => ({ useAuth: () => auth }))

type Status = Record<string, unknown>
const payload = (detail: Status) => ({ data: { detail } })

const connected = {
  configured: true,
  connected: true,
  status: 'ok',
  reason: 'Refresh token exchanged for an access token',
  checked_at: '2026-08-01T00:00:00+00:00',
  token_source: 'db',
  privacy_status: 'unlisted',
  runbook: 'docs/youtube-publishing.md',
}

function harness(ui: ReactNode) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(<QueryClientProvider client={client}>{ui}</QueryClientProvider>)
}

const badge = () => screen.getByTestId('youtube-status-badge')

beforeEach(() => {
  get.mockReset()
  auth = { sessionToken: 'tok', isAdmin: true }
})
afterEach(cleanup)

describe('YouTubePublishingCard (issue #742)', () => {
  it('reads the recorded probe and reports connected', async () => {
    get.mockResolvedValue(payload(connected))
    harness(<YouTubePublishingCard />)
    await waitFor(() => expect(badge().textContent).toBe('Connected'))
    expect(get.mock.calls[0][0]).toContain('live=false')
    expect(screen.getByText(/database \(rotatable without a deploy\)/i)).toBeTruthy()
  })

  it('reports needs re-auth with the reason and what it blocks', async () => {
    get.mockResolvedValue(
      payload({
        ...connected,
        connected: false,
        status: 'needs_reauth',
        reason: 'invalid_grant: Token has been expired or revoked',
        error: 'invalid_grant',
      }),
    )
    harness(<YouTubePublishingCard />)
    await waitFor(() => expect(badge().textContent).toBe('Needs re-auth'))
    expect(screen.getByText(/Token has been expired or revoked/)).toBeTruthy()
    expect(screen.getByText(/abort before spending/i)).toBeTruthy()
  })

  it('keeps an unreachable Google distinct from a dead grant', async () => {
    get.mockResolvedValue(payload({ ...connected, connected: false, status: 'unknown' }))
    harness(<YouTubePublishingCard />)
    // 'Unknown', not 'Needs re-auth' — only the second is something to act on.
    await waitFor(() => expect(badge().textContent).toBe('Unknown'))
  })

  it('re-probes on demand', async () => {
    get.mockResolvedValue(payload(connected))
    harness(<YouTubePublishingCard />)
    await waitFor(() => expect(badge()).toBeTruthy())
    fireEvent.click(screen.getByRole('button', { name: /check now/i }))
    await waitFor(() => expect(get.mock.calls.some((c) => String(c[0]).includes('live=true'))).toBe(true))
  })

  it('renders nothing when YouTube is not configured at all', async () => {
    get.mockResolvedValue(payload({ ...connected, configured: false, status: 'not_configured' }))
    const { container } = harness(<YouTubePublishingCard />)
    await waitFor(() => expect(get).toHaveBeenCalled())
    expect(container.textContent).toBe('')
  })

  it('never calls the admin endpoint for a non-admin', async () => {
    auth = { sessionToken: 'tok', isAdmin: false }
    const { container } = harness(<YouTubePublishingCard />)
    expect(get).not.toHaveBeenCalled()
    expect(container.textContent).toBe('')
  })
})
