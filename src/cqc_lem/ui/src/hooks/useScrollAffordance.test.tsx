import { describe, expect, it, afterEach } from 'vitest'
import { useCallback, useRef } from 'react'
import { act, cleanup, render, screen } from '@testing-library/react'
import { useScrollAffordance } from './useScrollAffordance'

interface Metrics {
  scrollHeight: number
  clientHeight: number
  scrollTop: number
}

// jsdom has no layout, so scrollHeight/clientHeight/scrollTop are all 0 — stamp them onto the
// instance the hook actually measures.
function stamp(node: HTMLElement, metrics: Metrics) {
  for (const [key, value] of Object.entries(metrics)) {
    Object.defineProperty(node, key, { value, configurable: true, writable: true })
  }
}

function Probe({ metrics }: { metrics: Metrics }) {
  const { attach: bind, canScrollUp, canScrollDown } = useScrollAffordance<HTMLDivElement>()
  const initial = useRef(metrics)
  // Stable identity: an inline ref callback would detach/re-attach on every render.
  const attach = useCallback(
    (node: HTMLDivElement | null) => {
      if (node) stamp(node, initial.current)
      bind(node)
    },
    [bind],
  )
  return (
    <>
      <div data-testid="scroller" ref={attach}>
        <ul>
          <li>row</li>
        </ul>
      </div>
      <span data-testid="up">{String(canScrollUp)}</span>
      <span data-testid="down">{String(canScrollDown)}</span>
    </>
  )
}

const flag = (id: string) => screen.getByTestId(id).textContent

afterEach(cleanup)

describe('useScrollAffordance (issue #583)', () => {
  it('reports no affordance when the content fits', () => {
    render(<Probe metrics={{ scrollHeight: 320, clientHeight: 320, scrollTop: 0 }} />)
    expect(flag('down')).toBe('false')
    expect(flag('up')).toBe('false')
  })

  it('reports more-below on first measure, without waiting for a scroll event', () => {
    render(<Probe metrics={{ scrollHeight: 900, clientHeight: 400, scrollTop: 0 }} />)
    expect(flag('down')).toBe('true')
    expect(flag('up')).toBe('false')
  })

  it('re-measures on scroll and drops the cue at the bottom', () => {
    render(<Probe metrics={{ scrollHeight: 900, clientHeight: 400, scrollTop: 0 }} />)
    const scroller = screen.getByTestId('scroller')

    stamp(scroller, { scrollHeight: 900, clientHeight: 400, scrollTop: 250 })
    act(() => {
      scroller.dispatchEvent(new Event('scroll'))
    })
    expect(flag('down')).toBe('true')
    expect(flag('up')).toBe('true')

    stamp(scroller, { scrollHeight: 900, clientHeight: 400, scrollTop: 500 })
    act(() => {
      scroller.dispatchEvent(new Event('scroll'))
    })
    expect(flag('down')).toBe('false')
    expect(flag('up')).toBe('true')
  })

  it('re-measures when the viewport resizes', () => {
    render(<Probe metrics={{ scrollHeight: 900, clientHeight: 900, scrollTop: 0 }} />)
    expect(flag('down')).toBe('false')

    stamp(screen.getByTestId('scroller'), { scrollHeight: 900, clientHeight: 300, scrollTop: 0 })
    act(() => {
      window.dispatchEvent(new Event('resize'))
    })
    expect(flag('down')).toBe('true')
  })

  it('ignores sub-pixel overflow', () => {
    render(<Probe metrics={{ scrollHeight: 320.4, clientHeight: 320, scrollTop: 0 }} />)
    expect(flag('down')).toBe('false')
  })

  it('unmounts cleanly with no ResizeObserver available (jsdom)', () => {
    expect(typeof ResizeObserver).toBe('undefined')
    const view = render(<Probe metrics={{ scrollHeight: 900, clientHeight: 400, scrollTop: 0 }} />)
    expect(() => view.unmount()).not.toThrow()
  })
})
