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

  // The card rounds its corners with `overflow-hidden`, so an eight-column table that overflows it
  // was CLIPPED on a phone — the Actions column simply did not exist (issue #894).
  it('puts the table in a scrollable region so the card cannot clip its columns', async () => {
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
    await waitFor(() => expect(screen.getByText('user@x.com')).toBeTruthy())
    const wrap = screen.getByTestId('feedback-table')
    expect(wrap.className).toContain('overflow-x-auto')
    expect(wrap.querySelector('table')).toBeTruthy()
    expect(parseInt((wrap.firstElementChild as HTMLElement).style.minWidth, 10)).toBeGreaterThan(430)
  })

  // Issue #1036: "clicking approve does nothing". Every approve outcome that files nothing leaves
  // the row in `new`, so the table re-renders identically — the message at the button IS the only
  // thing that happened, and it has to say so in words.
  it('reports an approve that reached no issue AT the button, not as a success', async () => {
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
    post.mockResolvedValue({ data: { detail: {
      reviewed: true, action: 'approved', filed: false,
      filing_result: { action: 'error', reason: 'issue creation failed' },
    } } })

    harness(<AdminFeedbackPage />)
    await waitFor(() => expect(screen.getByRole('button', { name: /approve/i })).toBeTruthy())
    fireEvent.click(screen.getByRole('button', { name: /approve/i }))

    await waitFor(() => expect(screen.getAllByText(/GitHub refused the filing/).length)
      .toBeGreaterThan(0))
    // Shown inside the row's actions cell, next to the button that was clicked.
    const inRow = screen.getByRole('button', { name: /approve/i })
      .closest('td')?.textContent || ''
    expect(inRow).toContain('GitHub refused the filing')
    expect(inRow).toContain('Try Approve again')
  })

  // Same `error` verb, different instruction: GitHub never saw this one. Reporting it as a refusal
  // would point the admin at the wrong system.
  it('says the classifier was unreachable rather than blaming GitHub', async () => {
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
    post.mockResolvedValue({ data: { detail: {
      reviewed: true, action: 'approved', filed: false,
      filing_result: { action: 'error', reason: 'classification unavailable' },
    } } })

    harness(<AdminFeedbackPage />)
    await waitFor(() => expect(screen.getByRole('button', { name: /approve/i })).toBeTruthy())
    fireEvent.click(screen.getByRole('button', { name: /approve/i }))

    await waitFor(() => expect(screen.getAllByText(/triage classifier could not be reached/).length)
      .toBeGreaterThan(0))
    expect(screen.queryByText(/GitHub refused/)).toBeNull()
  })

  it('names the issue a successful approve produced', async () => {
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
    post.mockResolvedValue({ data: { detail: {
      reviewed: true, action: 'approved', filed: true,
      filing_result: { action: 'filed', issue_number: 1036 },
    } } })

    harness(<AdminFeedbackPage />)
    await waitFor(() => expect(screen.getByRole('button', { name: /approve/i })).toBeTruthy())
    fireEvent.click(screen.getByRole('button', { name: /approve/i }))
    await waitFor(() => expect(screen.getAllByText(/Issue filed \(#1036\)/).length)
      .toBeGreaterThan(0))
  })

  it('sends the approve action for the row whose button was clicked', async () => {
    get.mockResolvedValue(listPayload([
      {
        id: 41,
        email: 'a@x.com',
        is_admin_reporter: false,
        source: 'widget',
        body: 'first',
        status: 'new',
        github_issue_number: null,
        created_at: '2026-07-29T12:00:00Z',
      },
      {
        id: 42,
        email: 'b@x.com',
        is_admin_reporter: false,
        source: 'widget',
        body: 'second',
        status: 'new',
        github_issue_number: null,
        created_at: '2026-07-29T12:00:00Z',
      },
    ]))
    post.mockResolvedValue({ data: { detail: {
      reviewed: true, action: 'approved', filed: true,
      filing_result: { action: 'filed', issue_number: 9 },
    } } })

    harness(<AdminFeedbackPage />)
    await waitFor(() => expect(screen.getAllByRole('button', { name: /approve/i }).length).toBe(2))
    fireEvent.click(screen.getAllByRole('button', { name: /approve/i })[1])
    await waitFor(() =>
      expect(post).toHaveBeenCalledWith('/admin/feedback/42/review', {
        session_token: 'tok',
        action: 'approve',
      })
    )
  })

  // Issue #1070: "the row never updates, even after a page refresh". The server persists
  // `issue_created` and the list endpoint returns it (locked server-side in
  // tests/unit/api/test_feedback_admin_api.py); this is the other half of that contract — the row
  // must take its buttons from the status it was HANDED, so a reload of a settled row shows none.
  it('offers no actions on a row the server reports as settled', async () => {
    get.mockResolvedValue(listPayload([
      {
        id: 45,
        email: 'user@x.com',
        is_admin_reporter: false,
        source: 'widget',
        type_hint: 'bug',
        body: 'Something is broken',
        status: 'issue_created',
        github_issue_number: 1068,
        created_at: '2026-07-29T12:00:00Z',
      },
    ]))
    harness(<AdminFeedbackPage />)
    await waitFor(() => expect(screen.getByText('user@x.com')).toBeTruthy())
    // Scoped to the row: the status filter renders every label as an <option> too.
    const row = screen.getByText('Something is broken').closest('tr') as HTMLElement
    expect(row.textContent).toContain('Issue created')
    expect(row.querySelector('a')?.textContent).toBe('#1068')
    expect(row.querySelectorAll('button').length).toBe(0)
    expect(screen.queryByRole('button', { name: /approve/i })).toBeNull()
    expect(screen.queryByRole('button', { name: /dismiss/i })).toBeNull()
  })

  // …and the same row settles WITHOUT a reload: the approve invalidates the list, and it is the
  // refetched status — not any local "I clicked it" state — that retires the buttons.
  it('retires the buttons when the refetched list reports the row as filed', async () => {
    const pending = {
      id: 45,
      email: 'user@x.com',
      is_admin_reporter: false,
      source: 'widget',
      type_hint: 'bug',
      body: 'Something is broken',
      status: 'new',
      github_issue_number: null,
      created_at: '2026-07-29T12:00:00Z',
    }
    get.mockResolvedValueOnce(listPayload([pending]))
    get.mockResolvedValue(listPayload([
      { ...pending, status: 'issue_created', github_issue_number: 1068 },
    ]))
    post.mockResolvedValue({ data: { detail: {
      reviewed: true, action: 'approved', filed: true,
      filing_result: { action: 'filed', issue_number: 1068 },
    } } })

    harness(<AdminFeedbackPage />)
    await waitFor(() => expect(screen.getByRole('button', { name: /approve/i })).toBeTruthy())
    fireEvent.click(screen.getByRole('button', { name: /approve/i }))

    await waitFor(() => expect(screen.queryByRole('button', { name: /approve/i })).toBeNull())
    const row = screen.getByText('Something is broken').closest('tr') as HTMLElement
    expect(row.textContent).toContain('Issue created')
    expect(row.querySelector('a')?.textContent).toBe('#1068')
    expect(screen.queryByRole('button', { name: /dismiss/i })).toBeNull()
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
    // Banner AND row (issue #1036) — the banner alone is above a table the admin has scrolled past.
    await waitFor(() =>
      expect(screen.getAllByText(/Feedback already triaged/).length).toBe(2)
    )
  })
})
