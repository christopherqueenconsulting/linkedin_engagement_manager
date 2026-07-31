import { describe, expect, it, vi, afterEach, beforeEach } from 'vitest'
import type { ReactNode } from 'react'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import LinkedInSessionCard from './LinkedInSessionCard'

const post = vi.fn()
vi.mock('../api/client', () => ({ default: { post: (...args: unknown[]) => post(...args) } }))
vi.mock('../contexts/AuthContext', () => ({ useAuth: () => ({ sessionToken: 'tok' }) }))

function harness(ui: ReactNode) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(<QueryClientProvider client={client}>{ui}</QueryClientProvider>)
}

const LI_AT = 'AQEDAReallyLongLinkedInSessionCookieValue123'
const paste = () =>
  fireEvent.change(screen.getByLabelText(/li_at cookie value/i), { target: { value: LI_AT } })
const save = () => fireEvent.click(screen.getByText('Save LinkedIn Session'))

beforeEach(() => {
  post.mockReset()
  post.mockResolvedValue({ data: { detail: 'ok' } })
})
afterEach(cleanup)

describe('LinkedInSessionCard cookie-only migration (issue #745)', () => {
  it('shows no migration prompt for an account that has no stored password', () => {
    harness(<LinkedInSessionCard />)
    expect(screen.queryByTestId('cookie-migration-notice')).toBeNull()
  })

  it('never asks to drop a password the account does not have', async () => {
    harness(<LinkedInSessionCard />)
    paste()
    save()
    await waitFor(() => expect(post).toHaveBeenCalled())
    expect(post.mock.calls[0][1]).toMatchObject({ li_at: LI_AT, drop_password: false })
  })

  it('prompts password-only accounts to switch, and drops the password by default', async () => {
    harness(<LinkedInSessionCard migrationNeeded />)
    expect(screen.getByTestId('cookie-migration-notice')).toBeTruthy()
    paste()
    save()
    await waitFor(() => expect(post).toHaveBeenCalled())
    expect(post.mock.calls[0][1]).toMatchObject({ drop_password: true })
    await waitFor(() =>
      expect(screen.getByText(/stored password was deleted/i)).toBeTruthy()
    )
  })

  it('keeps the password when the user unchecks the box', async () => {
    harness(<LinkedInSessionCard migrationNeeded />)
    fireEvent.click(screen.getByLabelText(/Delete my saved LinkedIn password/i))
    paste()
    save()
    await waitFor(() => expect(post).toHaveBeenCalled())
    expect(post.mock.calls[0][1]).toMatchObject({ drop_password: false })
  })

  it('does not send a too-short cookie value', () => {
    harness(<LinkedInSessionCard migrationNeeded />)
    fireEvent.change(screen.getByLabelText(/li_at cookie value/i), { target: { value: 'short' } })
    save()
    expect(post).not.toHaveBeenCalled()
  })
})
