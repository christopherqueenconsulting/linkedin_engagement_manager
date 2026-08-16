import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import type { ReactNode } from 'react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import AdminUsersPage from './AdminUsersPage'

const get = vi.fn()
const post = vi.fn()
vi.mock('../api/client', () => ({
  default: {
    get: (...args: unknown[]) => get(...args),
    post: (...args: unknown[]) => post(...args),
  },
}))

vi.mock('../contexts/useAuth', () => ({
  useAuth: () => ({ sessionToken: 'tok', isAdmin: true, user: { email: 'admin@x.com', userId: 1 } }),
}))

function harness(ui: ReactNode) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(<QueryClientProvider client={client}>{ui}</QueryClientProvider>)
}

function row(overrides: Record<string, unknown> = {}) {
  return {
    id: 5,
    email: 'member@x.com',
    linkedin_email: null,
    is_admin: false,
    admin_via_column: false,
    admin_via_allowlist: false,
    subscription_status: 'active',
    subscription_tier: 'professional',
    trial_ends_at: null,
    linkedin_connection_status: 'connected',
    last_login: '2026-08-10T12:00:00Z',
    signed_up_at: '2026-07-01T12:00:00Z',
    activated_at: null,
    ...overrides,
  }
}

function listPayload(items: unknown[], total = items.length) {
  return { data: { detail: { items, total, limit: 25, offset: 0 } } }
}

// Scoped to the table: the filter selects carry the same words ("Active", "Connected") as the
// badges, so an unscoped query would match a dropdown option instead of a row.
function table() {
  return within(screen.getByTestId('users-table'))
}

beforeEach(() => {
  get.mockReset()
  post.mockReset()
})
afterEach(cleanup)

describe('AdminUsersPage (issue #1450)', () => {
  it('renders the account list with its state columns', async () => {
    get.mockResolvedValue(listPayload([row()]))
    harness(<AdminUsersPage />)
    expect(screen.getByText('User management')).toBeTruthy()
    await waitFor(() => expect(table().getByText('member@x.com')).toBeTruthy())
    expect(table().getByText('Active')).toBeTruthy()
    expect(table().getByText('Connected')).toBeTruthy()
    expect(table().getByText('Make admin')).toBeTruthy()
  })

  it('badges an allowlist admin so the env-only case is visible before the click', async () => {
    get.mockResolvedValue(listPayload([
      row({ is_admin: true, admin_via_allowlist: true, admin_via_column: false }),
    ]))
    harness(<AdminUsersPage />)
    await waitFor(() => expect(screen.getByTestId('admin-badge-5')).toBeTruthy())
    expect(screen.getByTestId('admin-badge-5').textContent).toContain('(env)')
  })

  it('sends a grant with the target in the path', async () => {
    get.mockResolvedValue(listPayload([row()]))
    post.mockResolvedValue({ data: { detail: { user_id: 5, is_admin: true, changed: true } } })
    harness(<AdminUsersPage />)
    await waitFor(() => expect(table().getByText('Make admin')).toBeTruthy())
    fireEvent.click(table().getByText('Make admin'))
    await waitFor(() => expect(post).toHaveBeenCalledWith('/admin/users/5/admin', {
      session_token: 'tok', is_admin: true,
    }))
  })

  // Every guard (self-revoke, the last admin, an allowlist admin) leaves the table looking
  // identical, so the server's message IS the whole feedback and it has to land at the button.
  it('shows the server refusal next to the button that caused it', async () => {
    get.mockResolvedValue(listPayload([row({ id: 1, email: 'admin@x.com', is_admin: true })]))
    post.mockRejectedValue({
      response: { data: { detail: 'You cannot remove your own admin access. Ask another admin.' } },
    })
    harness(<AdminUsersPage />)
    await waitFor(() => expect(table().getByText('Remove admin')).toBeTruthy())
    fireEvent.click(table().getByText('Remove admin'))
    await waitFor(() =>
      expect(table().getByText(/cannot remove your own admin access/)).toBeTruthy())
  })

  it('passes the filters to the query', async () => {
    get.mockResolvedValue(listPayload([]))
    harness(<AdminUsersPage />)
    await waitFor(() => expect(get).toHaveBeenCalled())
    fireEvent.change(screen.getByLabelText('Search by email'), { target: { value: 'acme' } })
    fireEvent.change(screen.getByLabelText('LinkedIn'), { target: { value: 'expired' } })
    fireEvent.click(screen.getByLabelText('Admins only'))
    await waitFor(() => {
      const params = get.mock.calls[get.mock.calls.length - 1][1].params
      expect(params.q).toBe('acme')
      expect(params.linkedin_connection_status).toBe('expired')
      expect(params.is_admin).toBe(true)
    })
  })

  // `q` becomes an unindexable `LIKE '%…%'` over `users`, so a query per keystroke is a table
  // scan per keystroke — the search is debounced into the applied term like ContentStudio's.
  it('does not query on every keystroke while the operator is still typing', async () => {
    get.mockResolvedValue(listPayload([]))
    harness(<AdminUsersPage />)
    await waitFor(() => expect(get).toHaveBeenCalled())
    const box = screen.getByLabelText('Search by email')
    fireEvent.change(box, { target: { value: 'a' } })
    fireEvent.change(box, { target: { value: 'ac' } })
    fireEvent.change(box, { target: { value: 'acme' } })
    await waitFor(() => {
      const params = get.mock.calls[get.mock.calls.length - 1][1].params
      expect(params.q).toBe('acme')
    })
    const asked = get.mock.calls.map((c) => (c[1] as { params: { q?: string } }).params.q)
    expect(asked).not.toContain('a')
    expect(asked).not.toContain('ac')
  })

  it('keeps the drawer in its own full-width row, not inside the email cell', async () => {
    get.mockImplementation((url: string) => {
      if (url === '/admin/users') return Promise.resolve(listPayload([row()]))
      return Promise.resolve({ data: { detail: { ...row(), public_uid: 'pub-1' } } })
    })
    harness(<AdminUsersPage />)
    await waitFor(() => expect(table().getByText('member@x.com')).toBeTruthy())
    // The email cell holds only the toggle, before AND after opening — a 22-field detail nested
    // there would stretch that column to half the table and shift every other one sideways.
    const emailCell = table().getByText('member@x.com').closest('td') as HTMLElement
    fireEvent.click(table().getByText('member@x.com'))
    await waitFor(() => expect(table().getByText('pub-1')).toBeTruthy())
    expect(within(emailCell).queryByText('pub-1')).toBeNull()
    const drawerCell = table().getByText('pub-1').closest('td') as HTMLElement
    expect(drawerCell.getAttribute('colspan')).toBe('7')
  })

  it('fetches the detail on demand, not once per row up front', async () => {
    get.mockImplementation((url: string) => {
      if (url === '/admin/users') return Promise.resolve(listPayload([row()]))
      return Promise.resolve({
        data: {
          detail: {
            ...row(), public_uid: 'pub-1', city: 'Atlanta', country: 'US',
            timezone: 'America/New_York', auto_schedule_posts: true,
          },
        },
      })
    })
    harness(<AdminUsersPage />)
    await waitFor(() => expect(table().getByText('member@x.com')).toBeTruthy())
    expect(get).toHaveBeenCalledTimes(1)
    fireEvent.click(table().getByText('member@x.com'))
    await waitFor(() => expect(table().getByText('pub-1')).toBeTruthy())
    expect(table().getByText('Atlanta, US')).toBeTruthy()
  })

  it('reports an empty result rather than an empty table', async () => {
    get.mockResolvedValue(listPayload([], 0))
    harness(<AdminUsersPage />)
    await waitFor(() =>
      expect(table().getByText('No users match the current filters.')).toBeTruthy())
  })
})
