import { describe, expect, it, vi, afterEach, beforeEach } from 'vitest'
import type { ReactNode } from 'react'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import DailyReview from './DailyReview'
import { ageTone, bulkTargets, groupOf, highlightSegments, isSweepable } from './reviewHighlights'
import type { ReviewItem } from './reviewHighlights'

const get = vi.fn()
const post = vi.fn()
vi.mock('../../api/client', () => ({
  default: { get: (...a: unknown[]) => get(...a), post: (...a: unknown[]) => post(...a) },
}))
vi.mock('../../contexts/useAuth', () => ({ useAuth: () => ({ sessionToken: 'tok' }) }))
vi.mock('../../utils/analytics', () => ({ maskProps: (className: string) => ({ className }) }))

function item(over: Partial<ReviewItem>): ReviewItem {
  return {
    lane: 'scheduled_dms', id: 1, age_hours: 2, summary: 'DM to Ann', draft_text: 'hello there friend',
    hold_reason: null, risk_flags: [], confidence: 100, allowed_actions: ['approve', 'cancel'],
    edit_href: '/content?tab=dms', ...over,
  }
}

const CLEAN_A = item({ id: 1, summary: 'DM to Ann' })
const CLEAN_B = item({ id: 2, summary: 'DM to Bo', lane: 'connection_requests' })
const FLAGGED = item({
  id: 3, summary: 'DM to Cy', draft_text: 'We grew revenue 40% last year', risk_flags: ['number', 'first_person_claim'],
  confidence: 65,
})
const HELD = item({
  id: 4, lane: 'posts', summary: 'text post', hold_reason: 'Slop lint', allowed_actions: ['edit'],
  risk_flags: ['gate_near_miss'], age_hours: 60,
})

function mount(queue: ReviewItem[], ui: ReactNode = <DailyReview />) {
  get.mockResolvedValue({ data: { detail: queue } })
  post.mockImplementation((_url: string, body: { items: { lane: string; id: number }[] }) =>
    Promise.resolve({ data: { detail: body.items.map((i) => ({ ...i, ok: true, error: null })) } }))
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <MemoryRouter><QueryClientProvider client={client}>{ui}</QueryClientProvider></MemoryRouter>,
  )
}

beforeEach(() => {
  get.mockReset()
  post.mockReset()
})
afterEach(cleanup)

describe('reviewHighlights', () => {
  it('marks only flagged spans', () => {
    const segs = highlightSegments('We grew revenue 40% last year', ['number'])
    expect(segs.filter((s) => s.flagged).map((s) => s.text)).toEqual(['40%'])
    expect(segs.map((s) => s.text).join('')).toBe('We grew revenue 40% last year')
  })

  it('groups held before flagged before clean, and never sweeps flagged or held rows', () => {
    expect(groupOf(HELD)).toBe('held')
    expect(groupOf(FLAGGED)).toBe('check')
    expect(groupOf(CLEAN_A)).toBe('clean')
    expect([CLEAN_A, FLAGGED, HELD].filter(isSweepable)).toEqual([CLEAN_A])
    expect(isSweepable(item({ confidence: null }))).toBe(false)
    expect(isSweepable(item({ allowed_actions: ['edit'] }))).toBe(false)
  })

  it('tones age amber over 24h and red over 48h', () => {
    expect([null, 10, 25, 49].map(ageTone)).toEqual(['none', 'none', 'amber', 'red'])
  })
})

describe('DailyReview', () => {
  it('approve-all sends only unflagged, approvable items', async () => {
    mount([CLEAN_A, CLEAN_B, FLAGGED, HELD])
    const btn = await screen.findByRole('button', { name: 'Approve all 2 with no flags' })
    fireEvent.click(btn)
    await waitFor(() => expect(post).toHaveBeenCalledTimes(1))
    const body = post.mock.calls[0][1]
    expect(body.action).toBe('approve')
    expect(body.items).toEqual([
      { lane: 'scheduled_dms', id: 1 },
      { lane: 'connection_requests', id: 2 },
    ])
  })

  it('offers no sweep button when every item is flagged', async () => {
    mount([FLAGGED])
    await screen.findByText('DM to Cy')
    expect(screen.queryByRole('button', { name: /Approve all/ })).toBeNull()
  })

  it('shows age badge tone, hold reason inline and highlighted spans', async () => {
    mount([FLAGGED, HELD])
    await screen.findByText('DM to Cy')
    const held = screen.getByTestId('review-item-posts:4')
    expect(within(held).getByTestId('age-badge').getAttribute('data-tone')).toBe('red')
    expect(within(held).getByText(/Held: Slop lint/)).toBeDefined()
    const marks = screen.getByTestId('review-item-scheduled_dms:3').querySelectorAll('mark')
    expect(Array.from(marks).map((m) => m.textContent)).toContain('40%')
  })

  it('keyboard: j moves, e expands, a approves the current item, x cancels', async () => {
    mount([CLEAN_A, CLEAN_B])
    await screen.findByText('DM to Ann')
    const first = screen.getByTestId('review-item-scheduled_dms:1')
    expect(first.getAttribute('aria-current')).toBe('true')

    fireEvent.keyDown(window, { key: 'j' })
    const second = screen.getByTestId('review-item-connection_requests:2')
    expect(second.getAttribute('aria-current')).toBe('true')

    fireEvent.keyDown(window, { key: 'e' })
    expect(within(second).getByRole('button', { name: 'Collapse' })).toBeDefined()
    expect(second.querySelector('p.line-clamp-2')).toBeNull()

    fireEvent.keyDown(window, { key: 'k' })
    fireEvent.keyDown(window, { key: 'a' })
    await waitFor(() => expect(post).toHaveBeenCalledTimes(1))
    expect(post.mock.calls[0][1]).toMatchObject({ action: 'approve', items: [{ lane: 'scheduled_dms', id: 1 }] })

    fireEvent.keyDown(window, { key: 'x' })
    await waitFor(() => expect(post).toHaveBeenCalledTimes(2))
    expect(post.mock.calls[1][1].action).toBe('cancel')
  })

  it('keyboard: space selects, shift+a approves every clean item', async () => {
    mount([CLEAN_A, FLAGGED])
    await screen.findByText('DM to Ann')
    fireEvent.keyDown(window, { key: ' ' })
    expect(screen.getByRole('button', { name: 'Approve selected (1)' })).toBeDefined()
    fireEvent.keyDown(window, { key: 'A', shiftKey: true })
    await waitFor(() => expect(post).toHaveBeenCalledTimes(1))
    expect(post.mock.calls[0][1].items).toEqual([{ lane: 'scheduled_dms', id: 1 }])
  })

  it('ignores shortcuts while typing in a field', async () => {
    mount([CLEAN_A])
    const box = (await screen.findAllByRole('checkbox'))[0]
    fireEvent.keyDown(box, { key: 'a' })
    expect(post).not.toHaveBeenCalled()
  })

  it('offers undo only for scheduled DMs, and undo cancels them', async () => {
    mount([CLEAN_A, CLEAN_B])
    fireEvent.click(await screen.findByRole('button', { name: 'Approve all 2 with no flags' }))
    const undo = await screen.findByRole('button', { name: 'Undo 1 scheduled DM' })
    fireEvent.click(undo)
    await waitFor(() => expect(post).toHaveBeenCalledTimes(2))
    expect(post.mock.calls[1][1]).toMatchObject({ action: 'cancel', items: [{ lane: 'scheduled_dms', id: 1 }] })
  })

  it('funnel cancel asks first and says it aborts the whole funnel', async () => {
    const funnel = item({ id: 9, lane: 'outreach_funnel_targets', summary: 'Funnel dm: Di' })
    mount([funnel])
    fireEvent.click(await screen.findByRole('button', { name: 'Cancel' }))
    expect(post).not.toHaveBeenCalled()
    expect(screen.getByRole('alertdialog').textContent).toMatch(/aborts the whole funnel/)
    fireEvent.click(screen.getByRole('button', { name: 'Abort funnel' }))
    await waitFor(() => expect(post).toHaveBeenCalledTimes(1))
    expect(post.mock.calls[0][1].action).toBe('cancel')
  })

  it('reports per-item failures', async () => {
    mount([CLEAN_A])
    post.mockResolvedValue({
      data: { detail: [{ lane: 'scheduled_dms', id: 1, ok: false, error: 'not_waiting_for_review' }] },
    })
    fireEvent.click(await screen.findByRole('button', { name: 'Approve all 1 with no flags' }))
    expect((await screen.findByRole('status')).textContent).toMatch(/1 not done \(already handled\)/)
  })

  it('shows the empty state', async () => {
    mount([])
    expect(await screen.findByText('Nothing is waiting on you.')).toBeDefined()
  })
})

// MCP W2: a row the user's coding agent queued is badged and never part of a bulk approve.
describe('agent-created rows', () => {
  const AGENT = item({ id: 7, summary: 'DM to Agent pick', agent_created: true })

  it('never sweeps or multi-approves an agent-created row', () => {
    expect(isSweepable(AGENT)).toBe(false)
    // Outreach targets the agent added and catch-up drafts it rewrote are agent-created too.
    for (const lane of ['outreach_funnel_targets', 'catchup_touches']) {
      expect(isSweepable(item({ lane, agent_created: true }))).toBe(false)
      expect(isSweepable(item({ lane, agent_created: false }))).toBe(true)
    }
    expect(bulkTargets([AGENT, CLEAN_A], 'approve')).toEqual([CLEAN_A])
    expect(bulkTargets([AGENT], 'approve')).toEqual([AGENT])
    expect(bulkTargets([AGENT, CLEAN_A], 'cancel')).toEqual([AGENT, CLEAN_A])
  })

  it('badges the row and leaves it out of approve-all', async () => {
    mount([AGENT, CLEAN_A])
    const row = await screen.findByTestId('review-item-scheduled_dms:7')
    expect(within(row).getByTestId('agent-created-badge').textContent).toBe('Agent-created')
    expect(within(screen.getByTestId('review-item-scheduled_dms:1')).queryByTestId('agent-created-badge'))
      .toBeNull()
    fireEvent.click(screen.getByRole('button', { name: 'Approve all 1 with no flags' }))
    await waitFor(() => expect(post).toHaveBeenCalledTimes(1))
    expect(post.mock.calls[0][1].items).toEqual([{ lane: 'scheduled_dms', id: 1 }])
  })

  it('drops it from approve-selected but approves it on its own', async () => {
    mount([AGENT, CLEAN_A])
    await screen.findByText('DM to Agent pick')
    for (const box of screen.getAllByRole('checkbox')) fireEvent.click(box)
    fireEvent.click(screen.getByRole('button', { name: 'Approve selected (2)' }))
    await waitFor(() => expect(post).toHaveBeenCalledTimes(1))
    expect(post.mock.calls[0][1].items).toEqual([{ lane: 'scheduled_dms', id: 1 }])

    const row = screen.getByTestId('review-item-scheduled_dms:7')
    fireEvent.click(within(row).getByRole('button', { name: 'Approve' }))
    await waitFor(() => expect(post).toHaveBeenCalledTimes(2))
    expect(post.mock.calls[1][1]).toMatchObject({ action: 'approve', items: [{ lane: 'scheduled_dms', id: 7 }] })
  })

  it('names the server refusal for a multi-item approve', async () => {
    mount([CLEAN_A])
    post.mockResolvedValue({
      data: { detail: [{ lane: 'scheduled_dms', id: 1, ok: false, error: 'agent_created_approve_individually' }] },
    })
    fireEvent.click(await screen.findByRole('button', { name: 'Approve all 1 with no flags' }))
    expect((await screen.findByRole('status')).textContent).toMatch(/approve it on its own/)
  })
})
