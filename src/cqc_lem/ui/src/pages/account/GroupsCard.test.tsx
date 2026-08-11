import { describe, expect, it, vi, afterEach, beforeEach } from 'vitest'
import type { ReactNode } from 'react'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import GroupsCard from './GroupsCard'
import { SaveAllBar, SettingsSaveProvider } from './SettingsSaveContext'
import type { GroupPostDraft, UserGroup } from './types'

const get = vi.fn()
const put = vi.fn()
vi.mock('../../api/client', () => ({
  default: {
    get: (...args: unknown[]) => get(...args),
    put: (...args: unknown[]) => put(...args),
  },
}))
vi.mock('../../contexts/AuthContext', () => ({ useAuth: () => ({ sessionToken: 'tok' }) }))
vi.mock('../../utils/analytics', () => ({
  maskProps: (className: string) => ({ className }),
  capture: vi.fn(),
  EVENTS: { prefsSaved: 'prefs_saved' },
}))

const GROUPS: UserGroup[] = [
  { group_id: 'g1', group_name: 'AI Leaders', enabled: true, post_enabled: true, last_posted_at: '2026-07-28T15:00:00', is_next_post: false },
  { group_id: 'g2', group_name: 'Sales Pros', enabled: true, post_enabled: true, last_posted_at: null, is_next_post: true },
]

const DRAFT: GroupPostDraft = {
  id: 11, group_id: 'g2', group_name: 'Sales Pros', content: 'A useful insight.', status: 'ready',
}

/**
 * The card reads two endpoints, so answers are routed by URL rather than by call order. `groupsSeq`
 * is the sequence of group payloads (the last one repeats, which is what a refetch re-reads).
 */
const routeGet = (groupsSeq: UserGroup[][], draft: GroupPostDraft | null = null) => {
  const queue = [...groupsSeq]
  get.mockImplementation((url: string) => {
    if (String(url).startsWith('/user/group-post-draft')) return Promise.resolve({ data: { detail: draft } })
    const groups = queue.length > 1 ? queue.shift()! : queue[0]
    return Promise.resolve({ data: { detail: groups } })
  })
}

function harness(ui: ReactNode) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(<QueryClientProvider client={client}>{ui}</QueryClientProvider>)
}

/** The card as the settings page actually mounts it — inside the Save All / unsaved-changes registry. */
function savingHarness(ui: ReactNode) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <SettingsSaveProvider>{ui}<SaveAllBar /></SettingsSaveProvider>
    </QueryClientProvider>
  )
}

beforeEach(() => {
  get.mockReset()
  put.mockReset()
})
afterEach(cleanup)

describe('GroupsCard (issue #769)', () => {
  it('says a group post is original, not a duplicate or reshare of a feed post', async () => {
    routeGet([GROUPS])
    harness(<GroupsCard />)
    await waitFor(() => expect(screen.getByText('LinkedIn Groups')).toBeTruthy())
    expect(screen.getByText(/never duplicated, reshared or cross-posted/i)).toBeTruthy()
    expect(screen.getByText(/one original post into one group/i)).toBeTruthy()
  })

  it('names the group the next weekly post goes to', async () => {
    routeGet([GROUPS])
    harness(<GroupsCard />)
    await waitFor(() => expect(screen.getByText('Next group post: Sales Pros.')).toBeTruthy())
    expect(screen.getByText('Next post')).toBeTruthy()
  })

  it('drops the next-post claim once that group is switched off, before saving', async () => {
    routeGet([GROUPS])
    harness(<GroupsCard />)
    await waitFor(() => expect(screen.getByText('Next group post: Sales Pros.')).toBeTruthy())
    fireEvent.click(screen.getByLabelText('Post in Sales Pros'))
    expect(screen.getByText(/picked from the groups below when you save/i)).toBeTruthy()
    expect(screen.queryByText('Next post')).toBeNull()
  })

  it('drops the next-post claim when another group is switched ON, before saving', async () => {
    // A never-posted group jumps to the front of the rotation, so the group marked at mount is no
    // longer the answer — naming it anyway is the confusion this card exists to end.
    routeGet([[{ ...GROUPS[0], is_next_post: true },
                { ...GROUPS[1], post_enabled: false, is_next_post: false },
                { group_id: 'g3', group_name: 'Founders', enabled: true, post_enabled: false,
                  last_posted_at: null, is_next_post: false }]])
    harness(<GroupsCard />)
    await waitFor(() => expect(screen.getByText('Next group post: AI Leaders.')).toBeTruthy())
    fireEvent.click(screen.getByLabelText('Post in Founders'))
    expect(screen.getByText(/picked from the groups below when you save/i)).toBeTruthy()
    expect(screen.queryByText('Next post')).toBeNull()
  })

  it('adopts the rotation the server re-resolves after a save', async () => {
    routeGet([GROUPS,
              [{ ...GROUPS[0], is_next_post: true },
               { ...GROUPS[1], post_enabled: false, is_next_post: false }]])
    put.mockResolvedValue({ data: { detail: 'ok' } })
    harness(<GroupsCard />)
    await waitFor(() => expect(screen.getByText('Next group post: Sales Pros.')).toBeTruthy())
    fireEvent.click(screen.getByLabelText('Post in Sales Pros'))
    fireEvent.click(screen.getByRole('button', { name: /Save Group Settings/i }))
    await waitFor(() => expect(screen.getByText('Next group post: AI Leaders.')).toBeTruthy())
  })

  it('says nothing will be posted when no group is opted in', async () => {
    routeGet([GROUPS.map((g) => ({ ...g, post_enabled: false, is_next_post: false }))])
    harness(<GroupsCard />)
    await waitFor(() => expect(screen.getByText(/will not post in any group/i)).toBeTruthy())
  })

  it('saves commenting and posting as separate per-group choices', async () => {
    routeGet([GROUPS])
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
    routeGet([[{ group_id: 'g1', group_name: 'AI Leaders', enabled: true } as UserGroup]])
    harness(<GroupsCard />)
    await waitFor(() => expect(screen.getByLabelText('Post in AI Leaders')).toBeTruthy())
    expect(screen.getByLabelText('Post in AI Leaders').getAttribute('aria-checked')).toBe('true')
  })
})

describe('GroupsCard — group post preview/edit (issue #932)', () => {
  it('shows the actual text of the post that is going out, and which group it goes to', async () => {
    routeGet([GROUPS], DRAFT)
    harness(<GroupsCard />)
    await waitFor(() =>
      expect((screen.getByLabelText('Group post text') as HTMLTextAreaElement).value)
        .toBe('A useful insight.')
    )
    expect(screen.getByText('Next group post — Sales Pros')).toBeTruthy()
  })

  it('has nothing to preview when no post is queued', async () => {
    routeGet([GROUPS], null)
    harness(<GroupsCard />)
    await waitFor(() => expect(screen.getByText('LinkedIn Groups')).toBeTruthy())
    expect(screen.queryByLabelText('Group post text')).toBeNull()
  })

  it('saves the rewrite as the text that will be posted', async () => {
    routeGet([GROUPS], DRAFT)
    put.mockResolvedValue({ data: { detail: 'ok' } })
    harness(<GroupsCard />)
    await waitFor(() => expect(screen.getByLabelText('Group post text')).toBeTruthy())
    fireEvent.change(screen.getByLabelText('Group post text'), { target: { value: 'My own words.' } })
    fireEvent.click(screen.getByRole('button', { name: /Save post/i }))
    await waitFor(() =>
      expect(put).toHaveBeenCalledWith('/user/group-post-draft',
        { session_token: 'tok', content: 'My own words.' })
    )
  })

  it('skips this week rather than publishing something the user does not want', async () => {
    routeGet([GROUPS], DRAFT)
    put.mockResolvedValue({ data: { detail: 'ok' } })
    harness(<GroupsCard />)
    await waitFor(() => expect(screen.getByLabelText('Group post text')).toBeTruthy())
    fireEvent.click(screen.getByRole('button', { name: /Skip this week/i }))
    await waitFor(() =>
      expect(put).toHaveBeenCalledWith('/user/group-post-draft',
        { session_token: 'tok', status: 'skipped' })
    )
  })

  it('offers the way back into the queue on a skipped draft (issue #1224)', async () => {
    // Since #1224 the API keeps returning a skipped draft so it can be restored — this card must
    // not describe it as what LEM is about to publish, nor offer a second skip.
    routeGet([GROUPS], { ...DRAFT, status: 'skipped' })
    put.mockResolvedValue({ data: { detail: 'ok' } })
    harness(<GroupsCard />)
    await waitFor(() => expect(screen.getByLabelText('Group post text')).toBeTruthy())
    expect(screen.queryByRole('button', { name: /Skip this week/i })).toBeNull()
    expect(screen.getByText(/Skipped — nothing goes out this week/i)).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: /Put back in the queue/i }))
    await waitFor(() =>
      expect(put).toHaveBeenCalledWith('/user/group-post-draft',
        { session_token: 'tok', status: 'ready' })
    )
    expect(screen.getByText(/Back in the queue for this week\./i)).toBeTruthy()
  })

  it('will not save an emptied post — skipping is how you cancel', async () => {
    routeGet([GROUPS], DRAFT)
    harness(<GroupsCard />)
    await waitFor(() => expect(screen.getByLabelText('Group post text')).toBeTruthy())
    fireEvent.change(screen.getByLabelText('Group post text'), { target: { value: '   ' } })
    expect((screen.getByRole('button', { name: /Save post/i }) as HTMLButtonElement).disabled).toBe(true)
    expect((screen.getByRole('button', { name: /Skip this week/i }) as HTMLButtonElement).disabled).toBe(false)
  })

  it('will not save past the LinkedIn post cap', async () => {
    routeGet([GROUPS], DRAFT)
    harness(<GroupsCard />)
    await waitFor(() => expect(screen.getByLabelText('Group post text')).toBeTruthy())
    fireEvent.change(screen.getByLabelText('Group post text'), { target: { value: 'x'.repeat(3001) } })
    expect(screen.getByText('3001/3000')).toBeTruthy()
    expect((screen.getByRole('button', { name: /Save post/i }) as HTMLButtonElement).disabled).toBe(true)
  })

  // Without its own registration the rewrite is invisible to the page's save machinery: Save All
  // would report success having written only the toggles, and the unsaved guard would let the user
  // walk away — and the text they thought they had replaced is what publishes.
  it('counts an unsaved rewrite as unsaved changes, and Save All writes it', async () => {
    routeGet([GROUPS], DRAFT)
    put.mockResolvedValue({ data: { detail: 'ok' } })
    savingHarness(<GroupsCard />)
    await waitFor(() => expect(screen.getByLabelText('Group post text')).toBeTruthy())
    expect(screen.queryByRole('button', { name: /Save All/ })).toBeNull()
    fireEvent.change(screen.getByLabelText('Group post text'), { target: { value: 'My own words.' } })
    await waitFor(() => expect(screen.getByRole('button', { name: /Save All/ })).toBeTruthy())
    fireEvent.click(screen.getByRole('button', { name: /Save All/ }))
    await waitFor(() =>
      expect(put).toHaveBeenCalledWith('/user/group-post-draft',
        { session_token: 'tok', content: 'My own words.' })
    )
  })

  it('does not claim unsaved changes when the text still matches what is queued', async () => {
    routeGet([GROUPS], DRAFT)
    put.mockResolvedValue({ data: { detail: 'ok' } })
    savingHarness(<GroupsCard />)
    await waitFor(() => expect(screen.getByLabelText('Group post text')).toBeTruthy())
    fireEvent.change(screen.getByLabelText('Group post text'), { target: { value: 'A useful insight.' } })
    expect(screen.queryByRole('button', { name: /Save All/ })).toBeNull()
  })
})
