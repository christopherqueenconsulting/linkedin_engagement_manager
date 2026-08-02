import { describe, expect, it, vi, afterEach, beforeEach } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import LoginModal from './LoginModal'

const post = vi.fn()
const login = vi.fn()
vi.mock('../api/client', () => ({ default: { post: (...args: unknown[]) => post(...args) } }))
vi.mock('../contexts/AuthContext', () => ({
  useAuth: () => ({ closeLoginModal: vi.fn(), login: (...a: unknown[]) => login(...a) }),
}))
vi.mock('../utils/attribution', () => ({ getAttribution: () => ({}) }))
vi.mock('../utils/analytics', () => ({ recordSignup: vi.fn() }))
vi.mock('../utils/webauthn', () => ({
  isPasskeySupported: () => true,
  getPasskeyAssertion: vi.fn(async () => ({ id: 'cred' })),
}))

beforeEach(() => {
  post.mockReset()
  login.mockReset()
})
afterEach(cleanup)

async function submitEmailThenPin(pinResponse: Record<string, unknown>) {
  post.mockResolvedValueOnce({ data: { detail: { bypass: false, user_exists: true } } })
  render(<LoginModal />)
  fireEvent.change(screen.getByPlaceholderText('your@email.com'),
                   { target: { value: 'me@example.com' } })
  fireEvent.click(screen.getByText('Continue'))
  await waitFor(() => expect(screen.getByPlaceholderText('123456')).toBeTruthy())

  post.mockResolvedValueOnce({ data: { detail: pinResponse } })
  fireEvent.change(screen.getByPlaceholderText('123456'), { target: { value: '123456' } })
  fireEvent.click(screen.getByText('Sign In'))
}

describe('LoginModal — strong authentication (issue #745, phase 2c)', () => {
  it('signs in on the PIN alone when no strong factor is enrolled', async () => {
    await submitEmailThenPin({ session_token: 'tok', email: 'me@example.com', is_new_user: false })
    await waitFor(() => expect(login).toHaveBeenCalledWith('tok', 'me@example.com'))
  })

  it('asks for a second factor instead of signing in when one is enrolled', async () => {
    await submitEmailThenPin({ second_factor_required: true, pending_token: 'pending',
                               methods: ['totp', 'recovery_code'] })
    await waitFor(() => expect(screen.getByText('One more step')).toBeTruthy())
    expect(login).not.toHaveBeenCalled()
  })

  it('finishes the login with the authenticator code', async () => {
    await submitEmailThenPin({ second_factor_required: true, pending_token: 'pending',
                               methods: ['totp'] })
    await waitFor(() => expect(screen.getByLabelText('Authenticator code')).toBeTruthy())

    post.mockResolvedValueOnce({ data: { detail: { session_token: 'tok2', email: 'me@example.com' } } })
    fireEvent.change(screen.getByLabelText('Authenticator code'), { target: { value: '654321' } })
    fireEvent.click(screen.getByText('Sign In'))

    await waitFor(() => expect(login).toHaveBeenCalledWith('tok2', 'me@example.com'))
    const call = post.mock.calls.find((c) => c[0] === '/auth/second-factor/verify')
    expect(call?.[1]).toMatchObject({ pending_token: 'pending', method: 'totp', code: '654321' })
  })

  it('offers a recovery code as the way out of a lost device', async () => {
    await submitEmailThenPin({ second_factor_required: true, pending_token: 'pending',
                               methods: ['totp', 'recovery_code'] })
    await waitFor(() => expect(screen.getByText(/Lost your device/i)).toBeTruthy())
    fireEvent.click(screen.getByText(/Lost your device/i))
    expect(screen.getByLabelText('Recovery code')).toBeTruthy()
  })

  it('the bypass login is gated by the second factor too', async () => {
    post.mockResolvedValueOnce({
      data: { detail: { bypass: true, second_factor_required: true, pending_token: 'pending',
                        methods: ['totp'] } },
    })
    render(<LoginModal />)
    fireEvent.change(screen.getByPlaceholderText('your@email.com'),
                     { target: { value: 'me@example.com' } })
    fireEvent.click(screen.getByText('Continue'))

    await waitFor(() => expect(screen.getByText('One more step')).toBeTruthy())
    expect(login).not.toHaveBeenCalled()
  })

  it('signs in with a passkey without asking for an email at all', async () => {
    post
      .mockResolvedValueOnce({ data: { detail: { handle: 'h', options: {} } } })
      .mockResolvedValueOnce({ data: { detail: { session_token: 'tok3', email: 'me@example.com' } } })
    render(<LoginModal />)

    fireEvent.click(screen.getByText('Sign in with a passkey'))
    await waitFor(() => expect(login).toHaveBeenCalledWith('tok3', 'me@example.com'))
    expect(post.mock.calls[0][0]).toBe('/auth/passkey/login/begin')
    expect(post.mock.calls[0][1]).toEqual({})
  })
})
