import { describe, expect, it, vi, afterEach, beforeEach } from 'vitest'
import type { ReactNode } from 'react'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import ComposePost from './ComposePost'

// Issue #774: ComposePost is the surface a post is scheduled from, and its picker is a bare wall
// clock — converting it against a GUESSED zone stores an instant hours from what was picked. Two
// windows are covered here: the zone has never resolved (picker locked), and it resolved, a time
// was typed, and it went unresolved again — a session-token change re-keys the query, so the
// submit guard is reachable with a time already in the field.
const get = vi.fn()
const post = vi.fn()

vi.mock('../../api/client', () => ({
  default: {
    get: (...args: unknown[]) => get(...args),
    post: (...args: unknown[]) => post(...args),
  },
}))

vi.mock('../../contexts/AuthContext', () => ({
  useAuth: () => ({ user: { email: 'test@example.com', userId: 1 }, sessionToken: 'tok' }),
}))

// Mutable so a test can flip the zone from resolved to unresolved between renders.
let tzState = { timezone: 'UTC', isResolved: false }
vi.mock('../../hooks/useUserTimezone', () => ({
  useUserTimezone: () => tzState.timezone,
  useUserTimezoneState: () => tzState,
}))

vi.mock('../../utils/analytics', () => ({
  maskProps: (className: string) => ({ className }),
}))

vi.mock('../../components/LinkedInPostPreview', () => ({ default: () => null }))
vi.mock('../../components/TopicAuthorityMeter', () => ({ default: () => null }))

// One client for the whole test, so a re-render re-reads tzState without remounting the page.
let client: QueryClient

function tree(ui: ReactNode) {
  return (
    <MemoryRouter>
      <QueryClientProvider client={client}>{ui}</QueryClientProvider>
    </MemoryRouter>
  )
}

beforeEach(() => {
  get.mockReset()
  post.mockReset()
  tzState = { timezone: 'UTC', isResolved: false }
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  // Every query on this page reads r.data.detail; one shape satisfies all of them.
  get.mockResolvedValue({ data: { detail: { templates: [] } } })
})

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

const timeInput = (c: HTMLElement) => c.querySelector('input[type="datetime-local"]') as HTMLInputElement

describe('ComposePost scheduling with an unresolved timezone (issue #774)', () => {
  it('locks the picker and names the wait rather than the browser guess', () => {
    const { container } = render(tree(<ComposePost />))

    expect(timeInput(container).disabled).toBe(true)
    expect(screen.getByText(/loading your timezone/)).toBeDefined()
    expect(screen.queryByText('(UTC)')).toBeNull()
  })

  it('refuses to submit a time typed while the zone was known but lost before submit', () => {
    tzState = { timezone: 'America/New_York', isResolved: true }
    const { container, rerender } = render(tree(<ComposePost />))

    fireEvent.change(screen.getByPlaceholderText('What would you like to share?'), {
      target: { value: 'Hello world' },
    })
    fireEvent.change(timeInput(container), { target: { value: '2026-07-28T09:00' } })

    tzState = { timezone: 'UTC', isResolved: false }
    rerender(tree(<ComposePost />))

    fireEvent.click(screen.getByRole('button', { name: 'Save Draft' }))

    expect(post).not.toHaveBeenCalled()
    expect(screen.getByText(/timezone has not loaded yet/)).toBeDefined()
  })
})
