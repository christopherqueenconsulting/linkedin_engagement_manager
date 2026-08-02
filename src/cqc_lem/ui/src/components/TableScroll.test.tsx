import { describe, expect, it, afterEach } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'
import TableScroll from './TableScroll'

afterEach(cleanup)

function harness(props: Partial<Parameters<typeof TableScroll>[0]> = {}) {
  return render(
    <TableScroll label="Per-post performance" testId="wrap" {...props}>
      <table>
        <tbody><tr><td>cell</td></tr></tbody>
      </table>
    </TableScroll>,
  )
}

describe('TableScroll (issue #894)', () => {
  it('scrolls sideways rather than letting a phone squeeze the columns', () => {
    harness()
    expect(screen.getByTestId('wrap').className).toContain('overflow-x-auto')
  })

  it('holds a readable floor width for the table it wraps', () => {
    harness({ minWidth: 900 })
    const inner = screen.getByTestId('wrap').firstElementChild as HTMLElement
    expect(inner.style.minWidth).toBe('900px')
    expect(inner.querySelector('table')).toBeTruthy()
  })

  it('defaults to a floor wider than a phone, so the default is not a no-op', () => {
    harness()
    const inner = screen.getByTestId('wrap').firstElementChild as HTMLElement
    expect(parseInt(inner.style.minWidth, 10)).toBeGreaterThan(430)
  })

  // A scroll container reachable only by wheel or touch is unreachable by keyboard (WCAG 2.1.1).
  it('is a focusable, named region so the hidden columns are reachable without a pointer', () => {
    harness()
    const region = screen.getByRole('region', { name: 'Per-post performance' })
    expect(region.getAttribute('tabindex')).toBe('0')
  })

  it('keeps the caller-supplied layout classes alongside the scroll behaviour', () => {
    harness({ className: 'mt-2' })
    const cls = screen.getByTestId('wrap').className
    expect(cls).toContain('mt-2')
    expect(cls).toContain('overflow-x-auto')
  })
})
