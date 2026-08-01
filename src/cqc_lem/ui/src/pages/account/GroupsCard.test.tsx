import { describe, expect, it, vi, afterEach, beforeEach } from 'vitest'
import type { ReactNode } from 'react'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import GroupsCard from './GroupsCard'
import type { UserGroup } from './types'

const get = vi.fn()
const put = vi.fn()
vi.mock('../../api/client', () => ({
  default: {
    get: (...args: unknown[]) => get(...args),
    put: (...args: unknown[]) => put(...args),
  },
}))
vi.mock('../../contexts/AuthContext', () => ({ useAuth: () => ({ sessionToken: 'tok' }) }))

const GROUPS: UserGroup[] = [
  { group_id: 'g1', group_name: 'AI Leaders', enabled: true, post_enabled: true, last_posted_at: '2026-07-28T15:00:00', is_next_post: false },
  { group_id: 'g2', group_name: 'Sales Pros', enabled: true, post_enabled: true, last_posted_at: null, is_next_post: true },
]

function harness(ui: ReactNode) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(<QueryClientProvider client={client}>{ui}</QueryClientProvider>)
}

beforeEach(() => {
  get.mockReset()
  put.mockReset()
})
afterEach(cleanup)

describe('GroupsCard (issue #769)', () => {
  it('says a group post is original, not a duplicate or reshare of a feed post', async () => {
    get.mockResolvedValue({ data: { detail: GROUPS } })
    harness(<GroupsCard />)
    await waitFor(() => expect(screen.getByText('LinkedIn Groups')).toBeTruthy())
    expect(screen.getByText(/never duplicated, reshared or cross-posted/i)).toBeTruthy()
    expect(screen.getByText(/one original post into one group/i)).toBeTruthy()
  })

  it('names the group the next weekly post goes to', async () => {
    get.mockResolvedValue({ data: { detail: GROUPS } })
    harness(<GroupsCard />)
    await waitFor(() => expect(screen.getByText('Next group post: Sales Pros.')).toBeTruthy())
    expect(screen.getByText('Next post')).toBeTruthy()
  })

  it('drops the next-post claim once that group is switched off, before saving', async () => {
    get.mockResolvedValue({ data: { detail: GROUPS } })
    harness(<GroupsCard />)
    await waitFor(() => expect(screen.getByText('Next group post: Sales Pros.')).toBeTruthy())
    fireEvent.click(screen.getByLabelText('Post in Sales Pros'))
    expect(screen.getByText(/picked from the groups below when you save/i)).toBeTruthy()
    expect(screen.queryByText('Next post')).toBeNull()
  })

  it('drops the next-post claim when another group is switched ON, before saving', async () => {
    // A never-posted group jumps to the front of the rotation, so the group marked at mount is no
    // longer the answer — naming it anyway is the confusion this card exists to end.
    get.mockResolvedValue({
      data: {
        detail: [{ ...GROUPS[0], is_next_post: true },
                 { ...GROUPS[1], post_enabled: false, is_next_post: false },
                 { group_id: 'g3', group_name: 'Founders', enabled: true, post_enabled: false,
                   last_posted_at: null, is_next_post: false }],
      },
    })
    harness(<GroupsCard />)
    await waitFor(() => expect(screen.getByText('Next group post: AI Leaders.')).toBeTruthy())
    fireEvent.click(screen.getByLabelText('Post in Founders'))
    expect(screen.getByText(/picked from the groups below when you save/i)).toBeTruthy()
    expect(screen.queryByText('Next post')).toBeNull()
  })

  it('adopts the rotation the server re-resolves after a save', async () => {
    get.mockResolvedValueOnce({ data: { detail: GROUPS } }).mockResolvedValue({
      data: {
        detail: [{ ...GROUPS[0], is_next_post: true },
                 { ...GROUPS[1], post_enabled: false, is_next_post: false }],
      },
    })
    put.mockResolvedValue({ data: { detail: 'ok' } })
    harness(<GroupsCard />)
    await waitFor(() => expect(screen.getByText('Next group post: Sales Pros.')).toBeTruthy())
    fireEvent.click(screen.getByLabelText('Post in Sales Pros'))
    fireEvent.click(screen.getByRole('button', { name: /Save Group Settings/i }))
    await waitFor(() => expect(screen.getByText('Next group post: AI Leaders.')).toBeTruthy())
  })

  it('says nothing will be posted when no group is opted in', async () => {
    get.mockResolvedValue({
      data: { detail: GROUPS.map((g) => ({ ...g, post_enabled: false, is_next_post: false })) },
    })
    harness(<GroupsCard />)
    await waitFor(() => expect(screen.getByText(/will not post in any group/i)).toBeTruthy())
  })

  it('saves commenting and posting as separate per-group choices', async () => {
    get.mockResolvedValue({ data: { detail: GROUPS } })
    put.mockResolvedValue({ data: { detail: 'ok' } })
    harness(<GroupsCard />)
    await waitFor(() => expect(screen.getByLabelText('Comment in AI Leaders')).toBeTruthy())
    fireEvent.click(screen.getByLabelText('Comment in AI Leaders'))
    fireEvent.click(screen.getByLabelText('Post in Sales Pros'))
    fireEvent.click(screen.getByRole('button', { name: /Save Group Settings/i }))
    await waitFor(() =>
      expect(put).toHaveBeenCalledWith('/user/groups', {
        session_token: 'tok',
        groups: {
          g1: { enabled: false, post_enabled: true },
          g2: { enabled: true, post_enabled: false },
        },
      })
    )
  })

  it('treats a group payload with no post flag as opted in', async () => {
    get.mockResolvedValue({
      data: { detail: [{ group_id: 'g1', group_name: 'AI Leaders', enabled: true }] },
    })
    harness(<GroupsCard />)
    await waitFor(() => expect(screen.getByLabelText('Post in AI Leaders')).toBeTruthy())
    expect(screen.getByLabelText('Post in AI Leaders').getAttribute('aria-checked')).toBe('true')
  })
})
