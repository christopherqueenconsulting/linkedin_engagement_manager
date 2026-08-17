import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { AuthProvider } from './AuthContext'
import { useAuth } from './useAuth'
import { SESSION_ENDED_EVENT, SESSION_ENDED_MESSAGE } from '../utils/sessionEnd'

const get = vi.fn()
const post = vi.fn()
vi.mock('../api/client', () => ({
  default: {
    get: (...args: unknown[]) => get(...args),
    post: (...args: unknown[]) => post(...args),
  },
}))
vi.mock('../utils/analytics', () => ({ identifyUser: vi.fn(), resetAnalytics: vi.fn() }))

const writeFallbackSessionCookie = vi.fn()
const clearFallbackSessionCookie = vi.fn()
vi.mock('../utils/sessionCookie', () => ({
  SESSION_COOKIE_NAME: 'lem_session',
  writeFallbackSessionCookie: (...args: unknown[]) => writeFallbackSessionCookie(...args),
  clearFallbackSessionCookie: (...args: unknown[]) => clearFallbackSessionCookie(...args),
}))

function Probe() {
  const { user, sessionEndedReason, isLoginModalOpen, isLoading, login, logout } = useAuth()
  return (
    <div>
      <span data-testid="user">{user?.email ?? 'none'}</span>
      <span data-testid="reason">{sessionEndedReason ?? 'none'}</span>
      <span data-testid="modal">{isLoginModalOpen ? 'open' : 'closed'}</span>
      <span data-testid="loading">{isLoading ? 'loading' : 'ready'}</span>
      <button onClick={() => { login('real-token', 'user@example.com') }}>sign in</button>
      <button onClick={() => { void logout() }}>sign out</button>
    </div>
  )
}

function renderSignedIn() {
  localStorage.setItem('lem_session', 'cookie')
  localStorage.setItem('lem_email', 'user@example.com')
  localStorage.setItem('lem_li_connected', '1')
  get.mockResolvedValue({ data: { detail: { user_id: 7, email: 'user@example.com' } } })
  render(
    <QueryClientProvider client={new QueryClient()}>
      <AuthProvider><Probe /></AuthProvider>
    </QueryClientProvider>,
  )
  return waitFor(() => expect(screen.getByTestId('user').textContent).toBe('user@example.com'))
}

function renderSignedOut() {
  return render(
    <QueryClientProvider client={new QueryClient()}>
      <AuthProvider><Probe /></AuthProvider>
    </QueryClientProvider>,
  )
}

beforeEach(() => {
  get.mockReset()
  post.mockReset()
  writeFallbackSessionCookie.mockReset()
  clearFallbackSessionCookie.mockReset()
  localStorage.clear()
})
afterEach(() => { cleanup(); localStorage.clear() })

// Issue #1358. The teardown used to happen inside the axios interceptor — storage cleared and
// `window.location.href = '/'`, which discarded every piece of client state that could have said
// why. It is a state change here instead, and it carries a reason.
describe('a session that ended on its own', () => {
  it('signs the tab out and says why', async () => {
    await renderSignedIn()

    act(() => { window.dispatchEvent(new CustomEvent(SESSION_ENDED_EVENT)) })

    await waitFor(() => expect(screen.getByTestId('user').textContent).toBe('none'))
    expect(screen.getByTestId('reason').textContent).toBe(SESSION_ENDED_MESSAGE)
    // The sign-in surface is where the reason is read, so it has to be up.
    expect(screen.getByTestId('modal').textContent).toBe('open')
    expect(screen.getByTestId('loading').textContent).toBe('ready')
  })

  it('clears every per-browser key the deliberate sign-out clears', async () => {
    await renderSignedIn()

    act(() => { window.dispatchEvent(new CustomEvent(SESSION_ENDED_EVENT)) })

    await waitFor(() => expect(localStorage.getItem('lem_session')).toBeNull())
    expect(localStorage.getItem('lem_email')).toBeNull()
    expect(localStorage.getItem('lem_li_connected')).toBeNull()
  })

  it('tells the server nothing — the session it would revoke is the one that just proved gone', async () => {
    await renderSignedIn()

    act(() => { window.dispatchEvent(new CustomEvent(SESSION_ENDED_EVENT)) })

    await waitFor(() => expect(screen.getByTestId('user').textContent).toBe('none'))
    expect(post).not.toHaveBeenCalled()
  })
})

describe('a sign-out the user asked for', () => {
  it('carries no reason — they know why it happened', async () => {
    await renderSignedIn()
    post.mockResolvedValue({ data: {} })

    await act(async () => { screen.getByText('sign out').click() })

    await waitFor(() => expect(screen.getByTestId('user').textContent).toBe('none'))
    expect(screen.getByTestId('reason').textContent).toBe('none')
    expect(post).toHaveBeenCalledWith('/auth/logout', { session_token: 'cookie' })
  })
})

// Issue #1611. The fallback held the real token in localStorage only, which the ROUTE resolves (the
// `session_token` field) but the `/api` edge gate cannot see — it runs before routing and reads a
// bearer or the `lem_session` cookie. With API_ACCESS_TOKENS set that 401'd every panel in the app
// while the session itself was perfectly live. The fallback now carries the cookie too.
describe('the cookie-less login fallback', () => {
  it('writes the real token into the cookie the edge gate reads', async () => {
    renderSignedOut()
    // The cookie did not stick, so reading the session back on the sentinel fails.
    get.mockRejectedValue(new Error('401'))

    await act(async () => { screen.getByText('sign in').click() })

    await waitFor(() => expect(localStorage.getItem('lem_session')).toBe('real-token'))
    expect(writeFallbackSessionCookie).toHaveBeenCalledWith('real-token')
  })

  it('leaves a working cookie session alone — nothing is written for it', async () => {
    renderSignedOut()
    get.mockResolvedValue({ data: { detail: { user_id: 7, email: 'user@example.com' } } })

    await act(async () => { screen.getByText('sign in').click() })

    await waitFor(() => expect(screen.getByTestId('user').textContent).toBe('user@example.com'))
    expect(localStorage.getItem('lem_session')).toBe('cookie')
    expect(writeFallbackSessionCookie).not.toHaveBeenCalled()
  })

  it('re-writes it on every boot, before anything asks the API for anything', async () => {
    // A fallback session restored from storage: the browser that refused the server's cookie may
    // have dropped ours too, and a session stored by a bundle predating #1611 never had one.
    localStorage.setItem('lem_session', 'real-token')
    get.mockResolvedValue({ data: { detail: { user_id: 7, email: 'user@example.com' } } })

    renderSignedOut()

    await waitFor(() => expect(writeFallbackSessionCookie).toHaveBeenCalledWith('real-token'))
    // Order is the assertion: a cookie written after the first request is a request that 401s.
    expect(writeFallbackSessionCookie.mock.invocationCallOrder[0])
      .toBeLessThan(get.mock.invocationCallOrder[0])
  })

  it('writes nothing on a boot from the sentinel', async () => {
    await renderSignedIn()

    expect(writeFallbackSessionCookie).not.toHaveBeenCalled()
  })

  it('is cleared by a sign-out, like every other per-browser key', async () => {
    await renderSignedIn()
    post.mockResolvedValue({ data: {} })

    await act(async () => { screen.getByText('sign out').click() })

    await waitFor(() => expect(clearFallbackSessionCookie).toHaveBeenCalled())
  })

  it('is cleared by a session that ended on its own', async () => {
    await renderSignedIn()

    act(() => { window.dispatchEvent(new CustomEvent(SESSION_ENDED_EVENT)) })

    await waitFor(() => expect(clearFallbackSessionCookie).toHaveBeenCalled())
  })
})
