import { describe, expect, it, afterEach } from 'vitest'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import PerPostTable, { type PerPost } from './PerPostTable'

afterEach(cleanup)

function post(overrides: Partial<PerPost> = {}): PerPost {
  return {
    post_id: 1,
    scheduled_time: '2026-07-20T14:30:00',
    format: 'text',
    archetype: 'build_receipt',
    hook_style: 'contrarian_take',
    topic: 'ai',
    buyer_stage: 'awareness',
    reactions: 12,
    comments: 3,
    reposts: 1,
    saves: 2,
    impressions: 1500,
    engagement: 18,
    engagement_rate: 0.012,
    ...overrides,
  }
}

const toggle = () => screen.getByRole('button', { name: /Per-post performance/ })

describe('PerPostTable', () => {
  it('starts collapsed so the drill-down does not dominate the dashboard', () => {
    render(<PerPostTable posts={[post()]} />)
    expect(screen.queryByTestId('per-post-table')).toBeNull()
    expect(toggle().getAttribute('aria-expanded')).toBe('false')
  })

  it('says how many posts are behind the collapsed header', () => {
    render(<PerPostTable posts={[post({ post_id: 1 }), post({ post_id: 2 })]} />)
    expect(toggle().textContent).toContain('(2 posts)')
  })

  it('singularizes the count for one post', () => {
    render(<PerPostTable posts={[post()]} />)
    expect(toggle().textContent).toContain('(1 post)')
  })

  it('expands on click and collapses again', () => {
    render(<PerPostTable posts={[post()]} />)
    fireEvent.click(toggle())
    expect(screen.getByTestId('per-post-table')).toBeTruthy()
    expect(toggle().getAttribute('aria-expanded')).toBe('true')
    fireEvent.click(toggle())
    expect(screen.queryByTestId('per-post-table')).toBeNull()
  })

  it('renders a row per post with formatted values once expanded', () => {
    render(<PerPostTable posts={[post()]} />)
    fireEvent.click(toggle())
    const cells = screen.getByTestId('per-post-table').querySelectorAll('tbody td')
    expect([...cells].map((c) => c.textContent)).toEqual([
      'Jul 20',
      'Text',
      'Contrarian take',
      '1,500',
      '12',
      '3',
      '1',
      '2',
      '1.20%',
    ])
  })

  it('renders a missing date, format, impression count or rate as — rather than as zero', () => {
    render(
      <PerPostTable
        posts={[post({ scheduled_time: null, format: null, hook_style: null, impressions: null, engagement_rate: null })]}
      />,
    )
    fireEvent.click(toggle())
    const cells = [...screen.getByTestId('per-post-table').querySelectorAll('tbody td')].map((c) => c.textContent)
    expect(cells[0]).toBe('—')
    expect(cells[1]).toBe('—')
    expect(cells[2]).toBe('—')
    expect(cells[3]).toBe('—')
    expect(cells[8]).toBe('—')
  })

  // Nine columns inside a 375px phone: `w-full` alone squeezed each one to a sliver, which is what
  // the reporter of #894 saw. The wrapper scrolls instead and the table keeps a floor width.
  it('wraps the table in a scrollable region so a phone can read every column', () => {
    render(<PerPostTable posts={[post()]} />)
    fireEvent.click(toggle())
    const wrap = screen.getByTestId('per-post-table')
    expect(wrap.className).toContain('overflow-x-auto')
    expect(screen.getByRole('region', { name: /Per-post performance/ })).toBe(wrap)
    const inner = wrap.firstElementChild as HTMLElement
    expect(parseInt(inner.style.minWidth, 10)).toBeGreaterThan(430)
    expect(inner.querySelector('table')).toBeTruthy()
  })

  it('shows an empty note instead of a headers-only table when nothing was measured', () => {
    render(<PerPostTable posts={[]} />)
    expect(toggle().textContent).toContain('(0 posts)')
    fireEvent.click(toggle())
    expect(screen.queryByTestId('per-post-table')).toBeNull()
    expect(screen.getByTestId('per-post-empty')).toBeTruthy()
  })
})
