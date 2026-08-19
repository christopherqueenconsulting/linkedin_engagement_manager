import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import type { ReactNode } from 'react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import AdminAuditLogPage from './AdminAuditLogPage'

const get = vi.fn()
vi.mock('../api/client', () => ({
  default: {
    get: (...args: unknown[]) => get(...args),
  },
}))

vi.mock('../contexts/useAuth', () => ({
  useAuth: () => ({ sessionToken: 'tok', isAdmin: true }),
}))

function harness(ui: ReactNode) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(<QueryClientProvider client={client}>{ui}</QueryClientProvider>)
}

function entry(overrides: Record<string, unknown> = {}) {
  return {
    id: 1,
    user_id: 5,
    email: 'member@x.com',
    event: 'admin_granted',
    success: true,
    user_agent: 'curl',
    session_id: null,
    details: { actor_user_id: 1 },
    created_at: '2026-08-01T12:00:00Z',
    ...overrides,
  }
}

function page(items: unknown[], total = items.length) {
  return { data: { detail: { items, total, limit: 50, offset: 0 } } }
}

function table() {
  return within(screen.getByTestId('audit-log-table'))
}

beforeEach(() => get.mockReset())
afterEach(cleanup)

describe('AdminAuditLogPage (issue #1603)', () => {
  it('renders a labeled row', async () => {
    get.mockResolvedValue(page([entry()]))
    harness(<AdminAuditLogPage />)
    expect(screen.getByText('Audit log')).toBeTruthy()
    await waitFor(() => expect(table().getByText('Admin access granted')).toBeTruthy())
    expect(table().getByText('member@x.com')).toBeTruthy()
  })

  it('never renders ip_hash even if the payload carried one', async () => {
    get.mockResolvedValue(page([{ ...entry(), ip_hash: 'deadbeef' }]))
    harness(<AdminAuditLogPage />)
    await waitFor(() => expect(table().getByText('member@x.com')).toBeTruthy())
    expect(screen.queryByText(/deadbeef/)).toBeNull()
  })

  it('filters by user id', async () => {
    get.mockResolvedValue(page([]))
    harness(<AdminAuditLogPage />)
    await waitFor(() => expect(get).toHaveBeenCalled())
    fireEvent.change(screen.getByLabelText('Filter by user id'), { target: { value: '5' } })
    await waitFor(() => {
      const params = get.mock.calls[get.mock.calls.length - 1][1].params
      expect(params.user_id).toBe(5)
    })
  })

  it('reports an empty result rather than an empty table', async () => {
    get.mockResolvedValue(page([], 0))
    harness(<AdminAuditLogPage />)
    await waitFor(() =>
      expect(table().getByText('No audit rows match the current filter.')).toBeTruthy())
  })

  it('shows a refused event distinctly from a successful one', async () => {
    get.mockResolvedValue(page([entry({ event: 'step_up_denied', success: false })]))
    harness(<AdminAuditLogPage />)
    await waitFor(() => expect(table().getByText('Refused')).toBeTruthy())
  })
})
