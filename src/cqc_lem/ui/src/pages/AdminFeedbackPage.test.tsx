import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import type { ReactNode } from 'react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import AdminFeedbackPage from './AdminFeedbackPage'

const get = vi.fn()
const post = vi.fn()
vi.mock('../api/client', () => ({
  default: {
    get: (...args: unknown[]) => get(...args),
    post: (...args: unknown[]) => post(...args),
  },
}))

vi.mock('../contexts/AuthContext', () => ({
  useAuth: () => ({ sessionToken: 'tok', isAdmin: true }),
}))

function harness(ui: ReactNode) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(<QueryClientProvider client={client}>{ui}</QueryClientProvider>)
}

function listPayload(items: unknown[]) {
  return {
    data: {
      detail: {
        items,
        limit: 25,
        offset: 0,
      },
    },
  }
}

beforeEach(() => {
  get.mockReset()
  post.mockReset()
})
afterEach(cleanup)

describe('AdminFeedbackPage (issue #793)', () => {
  it('renders pending feedback and approve/dismiss buttons', async () => {
    get.mockResolvedValue(listPayload([
      {
        id: 1,
        email: 'user@x.com',
        is_admin_reporter: false,
        source: 'widget',
        type_hint: 'bug',
        body: 'Something is broken',
        status: 'new',
        github_issue_number: null,
        created_at: '2026-07-29T12:00:00Z',
      },
    ]))
    harness(<AdminFeedbackPage />)
    expect(screen.getByText('Feedback triage')).toBeTruthy()
    await waitFor(() => expect(screen.getByText('user@x.com')).toBeTruthy())
    expect(screen.getByRole('button', { name: /approve/i })).toBeTruthy()
    expect(screen.getByRole('button', { name: /dismiss/i })).toBeTruthy()
  })

  it('calls the dismiss endpoint and refreshes the list', async () => {
    get.mockResolvedValue(listPayload([
      {
        id: 1,
        email: 'user@x.com',
        is_admin_reporter: false,
        source: 'widget',
        type_hint: 'bug',
        body: 'Something is broken',
        status: 'new',
        github_issue_number: null,
        created_at: '2026-07-29T12:00:00Z',
      },
    ]))
    post.mockResolvedValue({ data: { detail: { reviewed: true, action: 'dismissed' } } })

    harness(<AdminFeedbackPage />)
    await waitFor(() => expect(screen.getByRole('button', { name: /dismiss/i })).toBeTruthy())
    fireEvent.click(screen.getByRole('button', { name: /dismiss/i }))
    await waitFor(() =>
      expect(post).toHaveBeenCalledWith('/admin/feedback/1/review', {
        session_token: 'tok',
        action: 'dismiss',
      })
    )
  })

  it('surfaces a failed review instead of swallowing it', async () => {
    get.mockResolvedValue(listPayload([
      {
        id: 1,
        email: 'user@x.com',
        is_admin_reporter: false,
        source: 'widget',
        type_hint: 'bug',
        body: 'Something is broken',
        status: 'new',
        github_issue_number: null,
        created_at: '2026-07-29T12:00:00Z',
      },
    ]))
    // 409: the beat filed this row between render and click.
    post.mockRejectedValue({ response: { data: { detail: 'Feedback already triaged (status issue_created)' } } })

    harness(<AdminFeedbackPage />)
    await waitFor(() => expect(screen.getByRole('button', { name: /approve/i })).toBeTruthy())
    fireEvent.click(screen.getByRole('button', { name: /approve/i }))
    await waitFor(() =>
      expect(screen.getByText(/Feedback already triaged/)).toBeTruthy()
    )
  })
})
